from typing import Optional, Union
from torch_geometric.typing import OptTensor, PairTensor, PairOptTensor

import torch
from torch import Tensor
from torch.nn import Linear
from torch_scatter import scatter
from torch_geometric.nn.conv import MessagePassing

import dgl


class GravNetConv(MessagePassing):
    r"""The GravNet operator from the `"Learning Representations of Irregular
    Particle-detector Geometry with Distance-weighted Graph
    Networks" <https://arxiv.org/abs/1902.07987>`_ paper, where the graph is
    dynamically constructed using nearest neighbors.
    The neighbors are constructed in a learnable low-dimensional projection of
    the feature space.
    A second projection of the input feature space is then propagated from the
    neighbors to each vertex using distance weights that are derived by
    applying a Gaussian function to the distances.
    Args:
        in_channels (int): The number of input channels.
        out_channels (int): The number of output channels.
        space_dimensions (int): The dimensionality of the space used to
           construct the neighbors; referred to as :math:`S` in the paper.
        propagate_dimensions (int): The number of features to be propagated
           between the vertices; referred to as :math:`F_{\textrm{LR}}` in the
           paper.
        k (int): The number of nearest neighbors.
        num_workers (int): Number of workers to use for k-NN computation.
            Has no effect in case :obj:`batch` is not :obj:`None`, or the input
            lies on the GPU. (default: :obj:`1`)
        **kwargs (optional): Additional arguments of
            :class:`torch_geometric.nn.conv.MessagePassing`.
    """

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        space_dimensions: int,
        propagate_dimensions: int,
        k: int,
        num_workers: int = 1,
        **kwargs
    ):
        super(GravNetConv, self).__init__(flow="target_to_source", **kwargs)

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.k = k
        self.num_workers = num_workers

        self.lin_s = Linear(in_channels, space_dimensions)
        self.lin_h = Linear(in_channels, propagate_dimensions)
        self.lin = Linear(in_channels + 2 * propagate_dimensions, out_channels)

        self.reset_parameters()

    def reset_parameters(self):
        self.lin_s.reset_parameters()
        self.lin_h.reset_parameters()
        self.lin.reset_parameters()

    def forward(self, g, x: Tensor, batch: OptTensor = None) -> Tensor:
        """"""

        assert x.dim() == 2, "Static graphs not supported in `GravNetConv`."

        b: OptTensor = None
        if isinstance(batch, Tensor):
            b = batch

        h_l: Tensor = self.lin_h(x)

        s_l: Tensor = self.lin_s(x)

        graph = knn_per_graph(g, s_l, self.k)
        graph.ndata['s_l'] = s_l
        row = graph.edges()[0]
        col = graph.edges()[0]
        edge_index = torch.stack([row, col], dim=0)

        edge_weight = (s_l[edge_index[1]] - s_l[edge_index[0]]).pow(2).sum(-1)
        edge_weight = torch.exp(-10.0 * edge_weight)  # 10 gives a better spread

        # propagate_type: (x: OptPairTensor, edge_weight: OptTensor)
        out = self.propagate(
            edge_index,
            x=(h_l, None),
            edge_weight=edge_weight,
            size=(s_l.size(0), s_l.size(0)),
        )

        return self.lin(torch.cat([out, x], dim=-1)), graph

    def message(self, x_j: Tensor, edge_weight: Tensor) -> Tensor:
        return x_j * edge_weight.unsqueeze(1)

    def aggregate(
        self, inputs: Tensor, index: Tensor, dim_size: Optional[int] = None
    ) -> Tensor:
        out_mean = scatter(
            inputs, index, dim=self.node_dim, dim_size=dim_size, reduce="mean"
        )
        out_max = scatter(
            inputs, index, dim=self.node_dim, dim_size=dim_size, reduce="max"
        )
        return torch.cat([out_mean, out_max], dim=-1)

    def __repr__(self):
        return "{}({}, {}, k={})".format(
            self.__class__.__name__, self.in_channels, self.out_channels, self.k
        )

def knn_per_graph(g, sl, k):
    graphs_list = dgl.unbatch(g)
    node_counter = 0
    new_graphs =[]
    for graph in graphs_list:
        non = graph.number_of_nodes()
        sls_graph = sl[node_counter:node_counter+non]
        new_graph = dgl.knn_graph(sls_graph, k, exclude_self=True)
        new_graphs.append(new_graph)
        node_counter = node_counter+non
    return dgl.batch(new_graphs)
    

        