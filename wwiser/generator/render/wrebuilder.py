import logging
from . import wrebuilder_util as ru


# Takes the parsed bank nodes and rebuilds them to simpler objects with quick access
# for main useful (sound) attributes, and has helper functions to write TXTP
# Used with registered HIRC objects, that are called by shortID (sub-objects like
# AkTree are handled per HIRC object).

#******************************************************************************

class Builder(object):
    def __init__(self):
        # nodes (default parser nodes) and bnodes (rebuilt simplified nodes)
        self._ref_to_node = {}              # bank + sid > parser node
        self._id_to_refs = {}               # sid > bank + sid list
        self._node_to_bnode = {}            # parser node > rebuilt node

        self._missing_nodes_loaded = {}     # missing nodes that should be in loaded banks (event garbage left by Wwise)
        self._missing_nodes_others = {}     # missing nodes in other banks (even pointing to other banks)
        self._missing_nodes_unknown = {}    # missing nodes of unknown type
        self._multiple_nodes = {}           # nodes that exist but were loaded in multiple banks and can't decide which one is best

        self._loaded_banks = {}             # id of banks that participate in generating
        self._missing_banks = {}            # banks missing in the "others" list
        self._unknown_props = {}            # object properties that need to be investigated
        self._transition_objects = 0        # info for future support

        # after regular generation we want a list of nodes that weren't used, and
        # generate TXTP for them, but ordered by types since generating some types
        # may end up using other unused types
        self._used_node = {}                # marks which node_refs has been used
        self._hircname_to_nodes = {}        # registered types > list of nodes

        return

    def set_filter(self, filter):
        self._filter = filter

    def get_missing_nodes_loaded(self):
        return self._missing_nodes_loaded

    def get_missing_nodes_others(self):
        return self._missing_nodes_others

    def get_missing_nodes_unknown(self):
        return self._missing_nodes_unknown

    def get_missing_banks(self):
        banks = list(self._missing_banks.keys())
        banks.sort()
        return banks

    def get_multiple_nodes(self):
        return self._multiple_nodes

    def get_transition_objects(self):
        return self._transition_objects

    def get_unknown_props(self):
        return self._unknown_props

    def report_unknown_props(self, unknowns):
        for unknown in unknowns:
            self._unknown_props[unknown] = True

    def report_transition_object(self):
        self._transition_objects += 1

    #--------------------------------------------------------------------------

    # info about loaded banks
    def add_loaded_bank(self, bank_id, bankname):
        self._loaded_banks[bank_id] = bankname

    #--------------------------------------------------------------------------

    # register a new node
    def add_node_ref(self, bank_id, sid, node):
        # Objects can be repeated when saved to different banks, and should be clones (ex. Magatsu Wahrheit, Ori ATWOTW).
        # Except sometimes they aren't, so we need to treat bank+id as separate things (ex. Detroit, Punch Out).
        # Doesn't seem allowed in Wwise but it's possible if devs manually load banks without conflicting ids.
        # ids may be in other banks though, so must also allow finding by single id

        ref = (bank_id, sid)

        if self._ref_to_node.get(ref) is not None:
            logging.debug("generator: ignored repeated bank %s + id %s", bank_id, sid)
            return
        self._ref_to_node[ref] = node

        if sid not in self._id_to_refs:
            self._id_to_refs[sid] = []
        self._id_to_refs[sid].append(ref)

        hircname = node.get_name()
        if hircname not in self._hircname_to_nodes:
            self._hircname_to_nodes[hircname] = []
        self._hircname_to_nodes[hircname].append(node)
        return

    def _get_node_by_ref(self, bank_id, sid):
        ref = (bank_id, sid)
        # find node in current bank
        node = self._ref_to_node.get(ref)
        # try node in another bank
        if not node:
            refs = self._id_to_refs.get(sid)
            if not refs:
                return None
            if len(refs) > 1:
                # could try to figure out if nodes are equivalent before reporting?
                logging.debug("generator: id %s found in multiple banks, not found in bank %s", sid, bank_id)
                self._multiple_nodes[sid] = True
            ref = refs[0]
            node = self._ref_to_node.get(ref)
        return node

    def _get_transition_node(self, ntid):
        # transition nodes in switches don't get used, register to generate at the end
        if not ntid:
            return None

        bank_id = ntid.get_root().get_id()
        tid = ntid.value()
        if not tid:
            return None

        node = self._get_node_by_ref(bank_id, tid)
        __ = self._get_bnode(node) #force parse/register (so doesn't appear as unused), but don't use yet
        return node


    def has_unused(self):
        # find if useful nodes where used
        for hirc_name in ru.UNUSED_HIRCS:
            nodes = self._hircname_to_nodes.get(hirc_name, [])
            for node in nodes:
                if id(node) not in self._used_node:
                    name = node.get_name()
                    #remove some false positives
                    if name == 'CAkMusicSegment':
                        #unused segments may not have child nodes (silent segments are ignored)
                        bnode = self._get_bnode(node, mark_used=False)
                        if bnode and bnode.ntids:
                            return True
        return False

    def get_unused_names(self):
        return ru.UNUSED_HIRCS

    def get_unused_list(self, hirc_name):
        results = []
        nodes = self._hircname_to_nodes.get(hirc_name, [])
        for node in nodes:
            if id(node) not in self._used_node:
                results.append(node)
        return results

    #--------------------------------------------------------------------------

    # Finds a rebuild node from a bank+id ref
    def _get_bnode_by_ref(self, bank_id, tid, sid_info=None, nbankid_info=None):
        if bank_id <= 0  or tid <= 0:
            # bank -1 seen in KOF12 bgm's play action referencing nothing
            return

        node = self._get_node_by_ref(bank_id, tid)
        if node:
            bnode = self._get_bnode(node)
        else:
            bnode = None

        if not bnode:
            # register info about missing node

            if nbankid_info:
                # when asked for a target bank
                if bank_id in self._loaded_banks:
                    if (bank_id, tid) not in self._missing_nodes_loaded: 
                        bankname = self._loaded_banks[bank_id]
                        logging.debug("generator: missing node %s in loaded bank %s, called by %s", tid, bankname, sid_info)

                    # bank is loaded: requested ID must be leftover garbage
                    self._missing_nodes_loaded[(bank_id, tid)] = True

                else:
                    bankname = nbankid_info.get_attr('hashname')
                    if not bankname:
                        bankname = str(nbankid_info.value())

                    if (bank_id, tid) not in self._missing_nodes_others:
                        logging.debug("generator: missing node %s in non-loaded bank %s, called by %s", tid, bankname, sid_info)

                    # bank not loaded: save bank name too
                    self._missing_nodes_others[(bank_id, tid)] = True
                    self._missing_banks[bankname] = True

            else:
                if (bank_id, tid) not in self._missing_nodes_unknown:
                    logging.debug("generator: missing node %s in unknown bank, called by %s", tid, sid_info)

                # unknown if node is in other bank or leftover garbage
                self._missing_nodes_unknown[(bank_id, tid)] = True

        return bnode

    # Takes a parser "node" and makes a rebuilt "bnode" for txtp use.
    # Normally would only need to rebuild per sid and ignore repeats (clones) in different banks, but
    # some games repeat sid for different objects in different banks (not clones), so just make one per node.
    def _get_bnode(self, node, mark_used=True):
        if not node:
            return None

        # check is node already in cache
        bnode = self._node_to_bnode.get(id(node))
        if bnode:
            return bnode

        # rebuild node with a helper class and save to cache
        # (some banks get huge and call the same things again and again, it gets quite slow to parse every time)
        hircname = node.get_name()
        bclass = ru.get_builder_hirc(hircname)

        bnode = bclass()
        bnode.init_builder(self)
        bnode.init_node(node)

        self._node_to_bnode[id(node)] = bnode
        if mark_used:
            self._used_node[id(node)] = True #register usage for unused detection
        return bnode
