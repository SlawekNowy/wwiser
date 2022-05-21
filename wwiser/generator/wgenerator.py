import logging
from . import wfilter, wmover, wtxtp_cache, wreport
from .render import wbuilder, wrenderer, wstate
from .registry import wgamesync
from .txtp import wtxtp



# Tries to write .txtp from a list of HIRC objects. Each object parser adds some part to final
# .txtp (like output name, or text info) and calls child objects, 'leaf' node(s) being some 
# source .wem in CAkSound or CAkMusicTrack.
#
# Nodes that don't contribute to audio are ignored. Objects may depend on variables, but some
# games use SetState and similar events, while others change via API. To unify both cases
# .txtp are created per possible variable combination, or variables may be pre-set via args.
#
# Output name is normally from event name (or number, if not defined), or first object found.
# .wem names are not used by default (given there can be multiple and that's how wwise works).
# Names and other info is written in the txtp.

#******************************************************************************

class Generator(object):
    def __init__(self, banks, wwnames=None):
        self._banks = banks

        self._builder = wbuilder.Builder()
        self._txtpcache = wtxtp_cache.TxtpCache()
        self._filter = wfilter.GeneratorFilter()  # filter nodes
        self._ws = wstate.WwiseState(self._txtpcache)
        self._renderer = wrenderer.Renderer(self._builder, self._ws, self._filter)
        self._mover = wmover.Mover(self._txtpcache)

        self._txtpcache.set_basepath(banks)
        self._txtpcache.wwnames = wwnames

        # options
        self._generate_unused = False       # generate unused after regular txtp
        self._move = False                  # move sources to wem dir
        self._bank_order = False            # use bank order to generate txtp (instead of prioritizing named nodes)

        self._default_hircs = self._renderer.get_generated_hircs()
        self._filter.set_default_hircs(self._default_hircs)
        self._builder.set_filter(self._filter)

    #--------------------------------------------------------------------------

    def set_filter(self, filter):
        self._filter.add(filter)

    def set_filter_rest(self, flag):
        self._filter.generate_rest = flag

    def set_filter_normal(self, flag):
        self._filter.skip_normal = flag

    def set_filter_unused(self, flag):
        self._filter.skip_unused = flag

    def set_bank_order(self, flag):
        self._bank_order = flag

    def set_generate_unused(self, generate_unused):
        if not generate_unused:
            return
        self._generate_unused = generate_unused

    def set_move(self, move):
        if not move:
            return
        self._move = move

    def set_gsparams(self, items):
        self._ws.set_gsdefaults(items)

    def set_gamevars(self, items):
        self._ws.set_gvdefaults(items)

    def set_renames(self, items):
        self._txtpcache.renamer.add(items)

    #--------------------------------------------------------------------------

    def set_outdir(self, path):
        if path is None:
            return
        self._txtpcache.outdir = self._txtpcache.normalize_path(path)

    def set_wemdir(self, path):
        if path is None:
            return
        self._txtpcache.wemdir = self._txtpcache.normalize_path(path)

    def set_master_volume(self, volume):
        self._txtpcache.set_master_volume(volume)

    def set_lang(self, flag):
        self._txtpcache.lang = flag

    def set_name_wems(self, flag):
        self._txtpcache.name_wems = flag

    def set_name_vars(self, flag):
        self._txtpcache.name_vars = flag

    def set_bnkskip(self, flag):
        self._txtpcache.bnkskip = flag

    def set_bnkmark(self, flag):
        self._txtpcache.bnkmark = flag

    def set_alt_exts(self, flag):
        self._txtpcache.alt_exts = flag

    def set_dupes(self, flag):
        self._txtpcache.dupes = flag

    def set_dupes_exact(self, flag):
        self._txtpcache.dupes_exact = flag

    def set_random_all(self, flag):
        self._txtpcache.random_all = flag

    def set_random_multi(self, flag):
        self._txtpcache.random_multi = flag

    def set_random_force(self, flag):
        self._txtpcache.random_force = flag

    def set_write_delays(self, flag):
        self._txtpcache.write_delays = flag

    def set_silence(self, flag):
        self._txtpcache.silence = flag

    def set_tags(self, tags):
        self._txtpcache.tags = tags
        tags.set_txtpcache(self._txtpcache)

    def set_x_noloops(self, flag):
        self._txtpcache.x_noloops = flag

    def set_x_nameid(self, flag):
        self._txtpcache.x_nameid = flag

    #--------------------------------------------------------------------------

    def generate(self):
        try:
            logging.info("generator: start")

            self._setup()
            self._write_normal()
            self._write_unused()
            self._report()

        except Exception: # as e
            logging.warn("generator: PROCESS ERROR! (report)")
            logging.exception("")
            raise
        return

    def _report(self):
        wreport.Report(self).report()


    def _setup(self):
        self._setup_nodes()
        self._txtpcache.mediaindex.load(self._banks)
        self._txtpcache.externals.load(self._banks)
        return

    def _setup_nodes(self):
        for bank in self._banks:
            root = bank.get_root()
            bank_id = root.get_id()
            bankname = bank.get_root().get_filename()

            self._builder.add_loaded_bank(bank_id, bankname)

            # register sids/nodes first since banks can point to each other
            items = bank.find(name='listLoadedItem')
            if not items: # media-only banks don't have items
                continue

            for node in items.get_children():
                nsid = node.find1(type='sid')
                if not nsid:
                    hircname = node.get_name()
                    logging.info("generator: not found for %s in %s", hircname, bankname) #???
                    continue
                sid = nsid.value()

                self._builder.register_node(bank_id, sid, node)

                # for nodes that can contain sources save them to move later
                if self._move:
                    self._mover.add_node(node)

        self._move_wems()
        return

    def _write_normal(self):
        # save nodes in bank order rather than all together (allows fine tuning bank load order)

        logging.info("generator: processing nodes")

        self._txtpcache.no_txtp = self._filter.skip_normal

        for bank in self._banks:
            self._write_bank(bank)

        self._txtpcache.no_txtp = False
        return

    def _write_bank(self, bank):
        items = bank.find(name='listLoadedItem')
        if not items:
            return

        nodes_allow = []
        nodes_named = []
        nodes_unnamed = []

        # save candidate nodes to generate
        nodes = items.get_children()
        for node in nodes:
            classname = node.get_name()
            nsid = node.find1(type='sid')
            if not nsid:
                continue
            #sid = nsid.value()

            # how nodes are accepted:
            # - filter not active: accept certain objects, and put them into named/unnamed lists (affects dupes)
            # - filter is active: accept allowed objects only, and put non-accepted into named/unnamed if "rest" flag is set (lower priority)
            allow = False
            if self._filter.active:
                allow = self._filter.allow_outer(node, nsid, classname=classname)
                if allow:
                    nodes_allow.append(node)
                    continue
                elif not self._filter.generate_rest:
                    continue # ignore non-"rest" nodes
                else:
                    pass #rest nodes are clasified below

            # include non-filtered nodes, or filtered but in rest
            if not allow and classname in self._default_hircs:
                allow = True

            if not allow:
                continue

            # put named nodes in a list to generate first, then unnamed nodes.
            # Useful when multiple events do the same thing, but we only have wwnames for one
            # (others may be leftovers). This way named ones are generated and others are ignored
            # as dupes. Can be disabled to treat all as unnamed = in bank order.
            hashname = nsid.get_attr('hashname')
            if hashname and not self._bank_order:
                item = (hashname, node)
                nodes_named.append(item)
            else:
                item = (nsid.value(), node)
                nodes_unnamed.append(item)

        # prepare nodes in final order
        nodes = []
        nodes += nodes_allow

        # usually gives better results with dupes
        # older python(?) may choke when trying to sort name+nodes, set custom handler to force hashname only
        nodes_named.sort(key=lambda x: x[0] )

        for __, node in nodes_named:
            nodes.append(node)
        for __, node in nodes_unnamed:
            nodes.append(node)

        logging.debug("generator: writting bank nodes (names: %s, unnamed: %s, filtered: %s)", len(nodes_named), len(nodes_unnamed), len(nodes_allow))

        # make txtp for nodes
        for node in nodes:
            self._render_txtp(node)

        return

    def _write_unused(self):
        if not self._generate_unused:
            return
        if not self._builder.has_unused():
            return

        # when filtering nodes without 'rest' (i.e. only generating a few nodes) can't generate unused,
        # as every non-filtered node would be considered so (generate first then add to filter list?)
        #if self._filter.active and not self._filter.generate_rest:
        #    return

        logging.info("generator: processing unused")
        if self._filter.active and not self._filter.generate_rest and not self._filter.has_unused():
            logging.info("generator: NOTICE! when using 'normal' filters must add 'unused' filters")

        self._txtpcache.no_txtp = self._filter.skip_unused
        self._txtpcache.stats.unused_mark = True

        for name in self._builder.get_unused_names():
            nodes = self._builder.get_unused_list(name)

            for node in nodes:

                allow = True
                if self._filter.active:
                    allow = self._filter.allow_unused(node)
                    #if self._filter.generate_rest: #?
                    #    allow = True

                if not allow:
                    continue

                self._render_txtp(node)

        self._txtpcache.stats.unused_mark = False
        self._txtpcache.no_txtp = False
        return


    # TXTP GENERATION
    # By default generator tries to make one .txtp per event. However, often there are multiple alts per
    # event that we want to generate:
    # - combinations of gamesyncs (states/switches)
    # - combinations of states in statechunks
    # - combinations of rtpc values
    # - variations of "selectable" wems (random1, then random2, etc)
    # - variations of externals that programmers can use to alter .wem on realtime
    # - transitions/stingers that could apply to current event
    # Generator goes step by step making combos of combos to cover all possible .txtp so the totals can be
    # huge (all those almost never happen at once).
    #
    # Base generation is "rendered" from current values, following Wwise's internals untils it creates a
    # rough tree that looks like a TXTP. This "rendering" varies depending on Wwise's internal state, meaning
    # you need to re-render when this state is different ('combinations'), as the objects it reaches change.
    # Changes that don't depend on state and that could be done by editting a .txtp manually are done 
    # post-rendering ('variations').
    #
    # Code below handles making 'combinations' (by chaining render_x calls), while code in Txtp handles
    # all 'variations' (by chaining write_x calls)

    def _render_txtp(self, node):
        try:
            self._render_base(node)

        except Exception: #as e
            sid = 0
            bankname = '?'
            nsid = node.find1(type='sid')
            if nsid:
                sid = nsid.value()
                bankname = nsid.get_root().get_filename()

            logging.info("generator: ERROR! node %s in %s", sid, bankname)
            raise

    # RENDER CHAINS:
    # Per each link, will try to find all possible combos. If there are combos,
    # we re-render again with each one for that link (which in turn may find combos for next link(s).
    # If no combo exists we skip to the next step (it's possible it didn't find GS combos but did SC combos).
    # 
    # example:
    # has GS combos bgm=m01/m02, SC combos layer=hi/lo, GV combos rank=N.N
    # - base (gets combos)
    #   - GS combo bgm=m01
    #     - SC combo layer=hi
    #       - GV combo rank=N.N
    #         - final txtp: "play_bgm (bgm=m01) {s}=(layer=hi) {rank=N.n}"
    #     - SC combo layer=lo
    #       - GV combo rank=N.N
    #         - final txtp: "play_bgm (bgm=m01) {s}=(layer=lo) {rank=N.n}"
    #   - GS combo bgm=m02
    #     - SC combo layer=hi
    #       - GV combo rank=N.N
    #         - final txtp: "play_bgm (bgm=m02) {s}=(layer=hi) {rank=N.n}"
    #     - SC combo layer=lo
    #       - GV combo rank=N.N
    #         - final txtp: "play_bgm (bgm=m02) {s}=(layer=lo) {rank=N.n}"
    #
    # base: no GS combos bgm=m01/m02, no SC combos, GV combos rank=N.N
    # - base (gets combos)
    #   - GS: none, skip
    #     - SC: none, skip
    #       - GV combo rank=N.n
    #         - final txtp: "play_bgm {rank=N.n}"
    #
    # It's possible to set defaults with CLI/GUI, in which case combos/params are fixed to certain values
    # and won't fill them during process or try multi-combos (outside those defined in config).
    # 
    # defaults: GS bgm=m01, SC layer=hi, GV rank=N.n
    # - base (gets combos)
    #   - GS combo bgm=m01
    #     - SC combo layer=hi
    #       - GV combo rank=N.N
    #         - final txtp: "play_bgm (bgm=m01) {s}=(layer=hi) {rank=N.n}"
    #
    # On each combo we need to reset next link's combos, as they may depend on previous config.
    # For example, by default it may find GS bgm=m01/m02, SC layer=hi/mid/lo (all possible combos of everything),
    # but actually GS bgm=m01 has SC layer=hi/mid and GS bgm=m02 has SC layer=mid/lo (would create fake dupes if
    # we just try all possible SCs every time).


    # handle new txtp with default parameters
    def _render_base(self, node):
        ncaller = node.find1(type='sid')

        self._ws.reset() #each new node starts from 0

        # initial render. if there are no combos this will be passed until final step
        txtp = wtxtp.Txtp(self._txtpcache)
        self._renderer.begin_txtp(txtp, node)

        self._render_gs(node, txtp)

        self._render_subs(ncaller)
        #TODO improve combos (unreachables doesn't make transitions?)


    # handle combinations of gamesyncs: "play_bgm (bgm=m01)", "play_bgm (bgm=m02)", ...
    def _render_gs(self, node, txtp):
        ws = self._ws

        # SCs have regular states and "unreachable" ones. If we have GS bgm=m01 and SC bgm=m01/m02,
        # m02 is technically not reachable (since GS states and SC states are the same thing).
        # Sometimes they are interesting so we want them, but *after* all regular SCs as they may be
        # duped of others.
        unreachables = []

        gscombos = ws.get_gscombos()
        if not gscombos:
            # no combo to re-render, skips to next step
            self._render_sc(node, txtp)

        else:
            # re-render with each combo
            for gscombo in gscombos:
                ws.set_gs(gscombo)
                ws.reset_sc()
                ws.reset_gv()

                txtp = wtxtp.Txtp(self._txtpcache)
                self._renderer.begin_txtp(txtp, node)

                ws.scpaths.filter(ws.gsparams, self._txtpcache.wwnames) #TODO improve
                if ws.scpaths.has_unreachables():
                    unreachables.append(gscombo)

                self._render_sc(node, txtp)
                #break


            for gscombo in unreachables:
                ws.set_gs(gscombo)
                ws.reset_sc()
                ws.reset_gv()

                txtp = wtxtp.Txtp(self._txtpcache)
                self._renderer.begin_txtp(txtp, node)

                self._render_sc(node, txtp, make_unreachables=True)



    # handle combinations of statechunks: "play_bgm (bgm=m01) {s}=(bgm_layer=hi)", "play_bgm (bgm=m01) {s}=(bgm_layer=lo)", ...
    def _render_sc(self, node, txtp, make_unreachables=False):
        ws = self._ws

        #TODO simplify: set scpaths to reachable/unreachable modes (no need to check sccombo_hash unreachables)

        if make_unreachables:
            ws.scpaths.filter(ws.gsparams, self._txtpcache.wwnames) #TODO improve
            #ws.scpaths.set_unreachables_only()

        sccombos = ws.get_sccombos() #found during process
        if not sccombos:
            # no combo to re-render, skips to next step
            self._render_gv(node, txtp)

        else:
            # re-render with each combo
            for sccombo in sccombos:
                if not make_unreachables and sccombo.has_unreachables(): #not ws.scpaths.is_unreachables_only():
                    continue
                if make_unreachables and not sccombo.has_unreachables(): #ws.scpaths.is_unreachables_only():
                    continue

                ws.set_sc(sccombo)
                ws.reset_gv()

                txtp = wtxtp.Txtp(self._txtpcache)
                txtp.scparams = sccombo #TODO remove
                self._renderer.begin_txtp(txtp, node)

                self._render_gv(node, txtp)


            # needs a base .txtp in some cases
            if not make_unreachables and ws.scpaths.generate_default(sccombos):
                ws.set_sc(None)
                ws.reset_gv()

                txtp = wtxtp.Txtp(self._txtpcache)
                txtp.scparams_make_default = True
                self.scparams = None
                self._renderer.begin_txtp(txtp, node)

                self._render_gv(node, txtp)

                txtp.scparams_make_default = False


    # handle combinations of gamevars: "play_bgm (bgm=m01) {s}=(bgm_layer=hi) {bgm_rank=2.0}"
    def _render_gv(self, node, txtp):
        ws = self._ws

        gvcombos = ws.get_gvcombos()
        if not gvcombos:
            # no combo to re-render, skips to next step
            self._render_last(node, txtp)

        else:
            # re-render with each combo
            for gvcombo in gvcombos:
                ws.set_gv(gvcombo)

                txtp = wtxtp.Txtp(self._txtpcache)
                self._renderer.begin_txtp(txtp, node)

                self._render_last(node, txtp)


    # final chain link
    def _render_last(self, node, txtp):
        txtp.write()


    def _render_subs(self, ncaller):
        ws = self._ws

        # stingers found during process
        bstingers = ws.stingers.get_items()
        if bstingers:
            for bstinger in bstingers:
                txtp = wtxtp.Txtp(self._txtpcache)
                self._renderer.begin_txtp_ntid(txtp, bstinger.ntid)
                txtp.set_ncaller(ncaller)
                txtp.set_bstinger(bstinger)
                txtp.write()

        # transitions found during process
        btransitions = ws.transitions.get_items()
        if btransitions:
            for btransition in btransitions:
                txtp = wtxtp.Txtp(self._txtpcache)
                self._renderer.begin_txtp_ntid(txtp, btransition.ntid)
                txtp.set_ncaller(ncaller)
                txtp.set_btransition(btransition)
                txtp.write()

    #--------------------------------------------------------------------------

    def _move_wems(self):
        self._mover.move_wems()
        return
