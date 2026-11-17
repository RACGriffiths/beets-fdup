"""Grouping pairwise matches into groups of items that all match each other (the default output, unless --by-track).

Each item is in one of three subsets, depending on which of the two queried sets it came from:
  'a': only in set 1 (the source items),
  'b': in both sets,
  'c': only in set 2 (the items they're compared against).
Items are only compared across the sets, so two 'a' items (or two 'c' items) are never matched with each other. A
group therefore needs each member to match every other member it was compared with, rather than every member."""

import collections
import math
from collections.abc import Iterable

from .matching import Match


def _edge_id(id1: int, id2: int) -> tuple[int, int]:
    return (id1, id2) if id1 < id2 else (id2, id1)


class Edge:
    id: tuple[int, int]
    weight: float

    def __init__(self, id: tuple[int, int], weight: float):
        self.id = id
        self.weight = weight


class Node:
    id: int
    conn_abc: dict[str, set[int]]   # ids of the items this one matches, by their subset
    weight_by_id: dict[int, float]  # match score with each of those
    abc: str                        # which subset this item is in

    def __init__(self, id: int, abc: str):
        self.id = id
        self.abc = abc
        self.conn_abc = collections.defaultdict(set[int])
        self.weight_by_id = {}


class Group:
    ids: set[int]
    ids_abc: dict[str, set[int]]      # ids in group indexed by whether in subset a b or c
    valid_abc: dict[str, set[int]]    # ids connected to all ids in each ids_abc
    potentials: dict[int, float]      # ids that could be added along with their minimum weight.
    order: list[int]                  # ids in the order they joined the group (the best matching pair first)
    score_by_id: dict[int, float]     # each member's weakest match with the members it was compared with

    def __init__(self):
        self.ids = set()
        self.ids_abc = collections.defaultdict(set[int])
        self.valid_abc = collections.defaultdict(set[int])
        self.potentials = collections.defaultdict(float)
        self.order = []
        self.score_by_id = {}

    def members(self) -> list[tuple[int, str]]:
        """Each member's id and subset: those in 'a' first, then 'b', then 'c', each in the order they joined."""
        return [(id_, abc) for abc in 'abc' for id_ in self.order if id_ in self.ids_abc[abc]]

    def _init_conn_abc(self, node: Node, abc: str):
        self.valid_abc[abc] = node.conn_abc[abc].copy()
        self.valid_abc[abc].difference_update(self.ids_abc[abc])  # Ensure we don't already have those ids.
        self.potentials.update({x: node.weight_by_id[x] for x in self.valid_abc[abc]})

    def _update_conn_abc(self, node: Node, abc: str):
        # Update min_weight for valid nodes also connected to new node.
        # Remove potential entirely for previously valid nodes that aren't connected to new node.
        for id in self.valid_abc[abc]:
            if id in node.conn_abc[abc]:
                if node.weight_by_id[id] < self.potentials[id]:
                    self.potentials[id] = node.weight_by_id[id]
            else:
                self.potentials.pop(id, None)
        self.valid_abc[abc] &= node.conn_abc[abc]

    def add_node(self, node: Node):

        # Update the scores: the new node's weakest match with the members it was compared with, and theirs.
        weights = [(id_, node.weight_by_id[id_]) for id_ in self.order if id_ in node.weight_by_id]
        for id_, weight in weights:
            self.score_by_id[id_] = min(self.score_by_id[id_], weight)
        self.score_by_id[node.id] = min((weight for _, weight in weights), default=math.inf)

        # Add node id:
        self.ids.add(node.id)
        self.ids_abc[node.abc].add(node.id)
        self.order.append(node.id)

        # If new node is 'b' or 'c', update valid 'a' nodes; initialise if this is first 'b' or 'c' node.
        if node.abc != 'a':
            if len(self.ids_abc['b']) + len(self.ids_abc['c']) == 1:
                self._init_conn_abc(node, 'a')
            else:
                self._update_conn_abc(node, 'a')

        # Always update valid 'b' nodes; initialise if this is first node in group.
        if len(self.ids) == 1:
            self._init_conn_abc(node, 'b')
        else:
            self._update_conn_abc(node, 'b')

        # If new node is 'a' or 'b', update valid 'c' nodes; initialise if this is first 'a' or 'b' node.
        if node.abc != 'c':
            if len(self.ids_abc['a']) + len(self.ids_abc['b']) == 1:
                self._init_conn_abc(node, 'c')
            else:
                self._update_conn_abc(node, 'c')

        # Remove the node from the valid groups (if still present)
        self.valid_abc[node.abc].discard(node.id)
        self.potentials.pop(node.id, None)
        self.potentials = dict(sorted(self.potentials.items(), key=lambda x: x[1]))


def group_matches(matches_by_id1: Iterable[tuple[int, list[Match]]], set1: set[int], set2: set[int]) -> list[Group]:
    """Group the matches (as yielded by find_matches). Each group starts from the best remaining match, then
    repeatedly adds the item whose weakest match with the group is strongest, while there is one that fits."""

    def subset(id_: int) -> str:
        return 'c' if id_ not in set1 else 'b' if id_ in set2 else 'a'

    # Loop over matches: turn into nodes and edges:
    edges_by_ids: dict[tuple[int, int], Edge] = {}
    nodes_by_id: dict[int, Node] = {}
    for id1, matches in matches_by_id1:
        for x in matches:

            # Create edge and nodes if they don't already exist
            eid = _edge_id(id1, x.id2)
            weight = x.overall_fit()
            edge = Edge(eid, weight)
            node1 = nodes_by_id.get(x.id1, None) or Node(x.id1, subset(x.id1))
            node2 = nodes_by_id.get(x.id2, None) or Node(x.id2, subset(x.id2))

            # Add edge to nodes.
            node1.conn_abc[node2.abc].add(x.id2)
            node2.conn_abc[node1.abc].add(x.id1)
            node1.weight_by_id[x.id2] = weight
            node2.weight_by_id[x.id1] = weight

            # Aggregate.
            edges_by_ids[eid] = edge
            nodes_by_id[x.id1] = node1
            nodes_by_id[x.id2] = node2

    # Sort edges by weight:
    edges_remaining = dict(sorted(edges_by_ids.items(), key=lambda x: x[1].weight))

    # Create groups until every edge is in one.
    groups: list[Group] = []
    while len(edges_remaining) > 0:

        # Pop the largest remaining edge:
        eid, edge = edges_remaining.popitem()

        # Create a new group with the nodes from this edge.
        group = Group()
        group.add_node(nodes_by_id[edge.id[0]])
        group.add_node(nodes_by_id[edge.id[1]])

        # Find which ids (if any) can be added to the group.
        while True:

            # If no nodes can be added, group is complete, move on.
            if len(group.potentials) == 0:
                break

            # Otherwise, find best potential node and to add to group.
            best_id, best_weight = group.potentials.popitem()
            to_add = nodes_by_id[best_id]

            # Add the new node to the group, and any edges in edges_remaining which are used can be removed.
            for _, conns in to_add.conn_abc.items():
                for id in conns:
                    if id in group.ids:
                        edges_remaining.pop(_edge_id(to_add.id, id), None)
            group.add_node(to_add)

        groups.append(group)

    return groups
