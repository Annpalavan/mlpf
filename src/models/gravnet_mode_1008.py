import torch
import torch.nn as nn
from torch import Tensor
from torch_scatter import scatter_min, scatter_max, scatter_mean, scatter_add

from src.layers.GravNetConv import GravNetConv

from typing import Tuple, Union, List
import dgl

from src.layers.object_cond import (
    calc_LV_Lbeta,
    get_clustering,
    calc_LV_Lbeta_inference,
)
from src.layers.obj_cond_inf import calc_energy_loss
from src.models.gravnet_model import (
    scatter_count,
    obtain_batch_numbers,
    global_exchange,
)


class GravNetBlock(nn.Module):
    def __init__(
        self,
        in_channels: int,
        space_dimensions: int = 3,
        k: int = 40,
        # batchnorm: bool = True
    ):
        super(GravNetBlock, self).__init__()
        # self.batchnorm = batchnorm
        # Includes all layers up to the global_exchange
        self.in_channels = in_channels
        propagate_dimensions = in_channels * 2
        out_channels = propagate_dimensions * 2
        self.gravnet_layer = GravNetConv(
            in_channels, out_channels, space_dimensions, propagate_dimensions, k
        ).jittable()
        self.post_gravnet = nn.Sequential(
            nn.Linear(out_channels + space_dimensions, in_channels),
            nn.ELU(),
            nn.Linear(in_channels, in_channels),
            nn.ELU(),
        )
        #! these are the two dense layers in line 199-205
        self.pre_gravnet = nn.Sequential(
            # nn.BatchNorm1d(out_channels),
            nn.Linear(in_channels, in_channels),
            nn.ELU(),
            nn.Linear(in_channels, in_channels),
            nn.ELU(),
        )
        self.output = nn.Sequential(
            nn.Linear(4 * in_channels, in_channels), nn.ELU()  # , nn.BatchNorm1d(96)
        )

    def forward(self, g, x: Tensor, batch: Tensor) -> Tensor:
        x, graph, s_l = self.gravnet_layer(g, x, batch)
        x = torch.cat((x, s_l), dim=1)  #! as in line 239 to also cat the coordinates
        x = self.post_gravnet(x)
        assert x.size(1) == self.in_channels
        x = global_exchange(x, batch)
        x = self.output(x)
        assert x.size(1) == self.in_channels
        return x, graph


class GravnetModel(nn.Module):
    def __init__(
        self,
        dev,
        input_dim: int = 9,
        output_dim: int = 31,
        n_postgn_dense_blocks: int = 4,
        n_gravnet_blocks: int = 4,
        clust_space_norm: str = "twonorm",
        k_gravnet: int = 7,
        activation: str = "elu",
    ):
        # if not batchnorm:
        #    print("!!!! no batchnorm !!!")
        super(GravnetModel, self).__init__()
        print("k_gravnet:", k_gravnet)
        # input_dim: int = 8
        # output_dim: int = 8 + 22  # 3x cluster positions, 1x beta, 3x position correction factor, 1x energy correction factor, 22x one-hot encoded particles (0th is the "OTHER" category)
        # n_gravnet_blocks: int = 4
        # n_postgn_dense_blocks: int = 4
        k = k_gravnet
        assert activation in ["relu", "tanh", "sigmoid", "elu"]
        acts = {
            "relu": nn.ReLU(),
            "tanh": nn.Tanh(),
            "sigmoid": nn.Sigmoid(),
            "elu": nn.ELU(),
        }
        self.act = acts[activation]
        N_NEIGHBOURS = [16, 128, 16, 256]

        self.return_graphs = False
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.n_gravnet_blocks = len(N_NEIGHBOURS)
        self.n_postgn_dense_blocks = n_postgn_dense_blocks
        # self.batchnorm = batchnorm
        # if self.batchnorm:
        self.batchnorm1 = nn.BatchNorm1d(self.input_dim)
        # else:
        #    self.batchnorm1 = nn.Identity()
        input_dim_prime = 9   # 4 * input_dim
        d_shape = 6  # 2 * input_dim
        self.input = nn.Linear(input_dim_prime, d_shape, bias=False)
        self.input.weight.data.copy_(torch.eye(d_shape, input_dim_prime))
        print("clust_space_norm", clust_space_norm)
        assert clust_space_norm in ["twonorm", "tanh", "none"]
        self.clust_space_norm = clust_space_norm
        # if isinstance(k, int):
        #     k = n_gravnet_blocks * [k]

        # assert len(k) == n_gravnet_blocks

        # Note: out_channels of the internal gravnet layer
        # not clearly specified in paper
        self.gravnet_blocks = nn.ModuleList(
            [
                GravNetBlock(d_shape, k=N_NEIGHBOURS[i])
                for i in range(self.n_gravnet_blocks)
            ]
        )

        # Post-GravNet dense layers
        postgn_dense_modules = nn.ModuleList()
        for i in range(self.n_postgn_dense_blocks):
            postgn_dense_modules.extend(
                [
                    nn.Linear(4 * d_shape if i == 0 else d_shape, d_shape),
                    self.act,  # ,
                    # nn.BatchNorm1d(128),
                ]
            )
        self.postgn_dense = nn.Sequential(*postgn_dense_modules)
        self.d_shape = d_shape
        # Output block
        self.output = nn.Sequential(
            nn.Linear(d_shape, 64),
            self.act,
            nn.Linear(64, 64),
            self.act,
            nn.Linear(64, 64),
        )

        self.post_pid_pool_module = nn.Sequential(  # to project pooled "particle type" embeddings to a common space
            nn.Linear(22, 64),
            self.act,
            nn.Linear(64, 64),
            self.act,
            nn.Linear(64, 22),
            nn.Softmax(dim=-1),
        )
        self.clustering = nn.Linear(64, self.output_dim - 1)
        self.beta = nn.Linear(64, 1)

    def forward(self, g):
        x = g.ndata["h"]
        device = x.device
        batch = obtain_batch_numbers(x, g)
        # print('forward called on device', device)
        # x = self.batchnorm1(x)
        # x = global_exchange(x, batch)
        #x_prime = XYZtoXYZPrime(x)
        #x = torch.cat((x, x_prime), dim=1)
        x = self.input(x)
        assert x.device == device

        x_gravnet_per_block = []  # To store intermediate outputs
        graphs = []
        for gravnet_block in self.gravnet_blocks:
            x, graph = gravnet_block(g, x, batch)
            x_gravnet_per_block.append(x)
            graphs.append(graph)
        x = torch.cat(x_gravnet_per_block, dim=-1)
        assert x.size() == (x.size(0), 4 * self.d_shape)
        assert x.device == device

        x = self.postgn_dense(x)
        x = self.output(x)
        x_cluster_coord = self.clustering(x)
        beta = self.beta(x)
        x = torch.cat((x_cluster_coord, beta.view(-1, 1)), dim=1)
        assert x.device == device
        if self.return_graphs:
            return x, graphs
        else:
            return x

    def object_condensation_loss2(
        self,
        batch,
        pred,
        y,
        return_resolution=False,
        clust_loss_only=False,
        add_energy_loss=False,
        calc_e_frac_loss=False,
        q_min=0.1,
        frac_clustering_loss=0.1,
        attr_weight=1.0,
        repul_weight=1.0,
        fill_loss_weight=0.0,
        use_average_cc_pos=0.0,
    ):
        """

        :param batch:
        :param pred:
        :param y:
        :param return_resolution: If True, it will only output resolution data to plot for regression (only used for evaluation...)
        :param clust_loss_only: If True, it will only add the clustering terms to the loss
        :return:
        """
        _, S = pred.shape
        if clust_loss_only:
            clust_space_dim = self.output_dim - 1
        else:
            clust_space_dim = self.output_dim - 28

        # xj = torch.nn.functional.normalize(
        #     pred[:, 0:clust_space_dim], dim=1
        # )  # 0, 1, 2: cluster space coords

        bj = torch.sigmoid(torch.reshape(pred[:, clust_space_dim], [-1, 1]))  # 3: betas
        original_coords = batch.ndata["h"][:, 0:clust_space_dim]
        xj = pred[:, 0:clust_space_dim]  # xj: cluster space coords
        if self.clust_space_norm == "twonorm":
            xj = torch.nn.functional.normalize(
                xj, dim=1
            )  # 0, 1, 2: cluster space coords
        elif self.clust_space_norm == "tanh":
            xj = torch.tanh(xj)
        elif self.clust_space_norm == "none":
            pass
        else:
            raise NotImplementedError
        if clust_loss_only:
            distance_threshold = torch.zeros((xj.shape[0], 3)).to(xj.device)
            energy_correction = torch.zeros_like(bj)
            momentum = torch.zeros_like(bj)
            pid_predicted = torch.zeros((distance_threshold.shape[0], 22)).to(
                momentum.device
            )
        else:
            distance_threshold = torch.reshape(
                pred[:, 1 + clust_space_dim : 4 + clust_space_dim], [-1, 3]
            )  # 4, 5, 6: distance thresholds
            energy_correction = torch.nn.functional.relu(
                torch.reshape(pred[:, 4 + clust_space_dim], [-1, 1])
            )  # 7: energy correction factor
            momentum = torch.nn.functional.relu(
                torch.reshape(pred[:, 27 + clust_space_dim], [-1, 1])
            )
            pid_predicted = pred[
                :, 5 + clust_space_dim : 27 + clust_space_dim
            ]  # 8:30: predicted particle one-hot encoding
        dev = batch.device
        clustering_index_l = batch.ndata["particle_number"]

        len_batch = len(batch.batch_num_nodes())
        batch_numbers = torch.repeat_interleave(
            torch.range(0, len_batch - 1).to(dev), batch.batch_num_nodes()
        ).to(dev)

        a = calc_LV_Lbeta(
            original_coords,
            batch,
            y,
            distance_threshold,
            energy_correction,
            momentum=momentum,
            predicted_pid=pid_predicted,
            beta=bj.view(-1),
            cluster_space_coords=xj,  # Predicted by model
            cluster_index_per_event=clustering_index_l.view(
                -1
            ).long(),  # Truth hit->cluster index
            batch=batch_numbers.long(),
            qmin=q_min,
            return_regression_resolution=return_resolution,
            post_pid_pool_module=self.post_pid_pool_module,
            clust_space_dim=clust_space_dim,
            frac_combinations=frac_clustering_loss,
            attr_weight=attr_weight,
            repul_weight=repul_weight,
            fill_loss_weight=fill_loss_weight,
            use_average_cc_pos=use_average_cc_pos,
        )
        if return_resolution:
            return a
        if clust_loss_only:
            loss = a[0] + a[1]
            if calc_e_frac_loss:
                loss_E_frac, loss_E_frac_true = calc_energy_loss(
                    batch, xj, bj.view(-1), qmin=q_min
                )
            if add_energy_loss:
                loss += a[2]  # TODO add weight as argument

        else:
            loss = (
                a[0]
                + a[1]
                + 20 * a[2]
                + 0.001 * a[3]
                + 0.001 * a[4]
                + 0.001
                * a[
                    5
                ]  # TODO: the last term is the PID classification loss, explore this yet
            )  # L_V / batch_size, L_beta / batch_size, loss_E, loss_x, loss_particle_ids, loss_momentum, loss_mass)
        if clust_loss_only:
            if calc_e_frac_loss:
                return loss, a, loss_E_frac, loss_E_frac_true
            else:
                return loss, a, 0, 0
        return loss, a, 0, 0

    def object_condensation_inference(self, batch, pred):
        """
        Similar to object_condensation_loss, but made for inference
        """
        _, S = pred.shape
        xj = torch.nn.functional.normalize(
            pred[:, 0:3], dim=1
        )  # 0, 1, 2: cluster space coords
        bj = torch.sigmoid(torch.reshape(pred[:, 3], [-1, 1]))  # 3: betas
        distance_threshold = torch.reshape(
            pred[:, 4:7], [-1, 3]
        )  # 4, 5, 6: distance thresholds
        energy_correction = torch.nn.functional.relu(
            torch.reshape(pred[:, 7], [-1, 1])
        )  # 7: energy correction factor
        momentum = torch.nn.functional.relu(
            torch.reshape(pred[:, 30], [-1, 1])
        )  # momentum magnitude
        pid_predicted = pred[:, 8:30]  # 8:30: predicted particle PID
        clustering_index = get_clustering(bj, xj)
        dev = batch.device
        len_batch = len(batch.batch_num_nodes())
        batch_numbers = torch.repeat_interleave(
            torch.range(0, len_batch - 1).to(dev), batch.batch_num_nodes()
        ).to(dev)

        pred = calc_LV_Lbeta_inference(
            batch,
            distance_threshold,
            energy_correction,
            momentum=momentum,
            predicted_pid=pid_predicted,
            beta=bj.view(-1),
            cluster_space_coords=xj,  # Predicted by model
            cluster_index_per_event=clustering_index.view(
                -1
            ).long(),  # Predicted hit->cluster index, determined by the clustering
            batch=batch_numbers.long(),
            qmin=0.1,
            post_pid_pool_module=self.post_pid_pool_module,
        )
        return pred


# class NoiseFilterModel(nn.Module):
#     def __init__(
#         self,
#         input_dim: int = 5,
#         output_dim: int = 2,
#     ):
#         super(NoiseFilterModel, self).__init__()
#         self.input_dim = input_dim
#         self.output_dim = output_dim
#         self.network = nn.Sequential(
#             nn.BatchNorm1d(self.input_dim),
#             nn.Linear(input_dim, 64),
#             nn.ReLU(),
#             nn.Linear(64, 32),
#             nn.ReLU(),
#             nn.Linear(32, 16),
#             nn.ReLU(),
#             nn.Linear(16, 2),
#             nn.LogSoftmax(),
#         )

#     def forward(self, x: Tensor) -> Tensor:
#         return self.network(x)


# class GravnetModelWithNoiseFilter(nn.Module):
#     def __init__(self, *args, **kwargs):
#         super(GravnetModelWithNoiseFilter, self).__init__()
#         self.signal_threshold = kwargs.pop("signal_threshold", 0.5)
#         self.gravnet = GravnetModel(*args, **kwargs)
#         self.noise_filter = NoiseFilterModel(input_dim=self.gravnet.input_dim)

#     def forward(self, x: Tensor, batch: Tensor) -> Tuple[Tensor, Tensor, Tensor]:
#         out_noise_filter = self.noise_filter(x)
#         pass_noise_filter = torch.exp(out_noise_filter[:, 1]) > self.signal_threshold
#         # Get the GravNet model output on only hits that pass the noise threshold
#         out_gravnet = self.gravnet(x[pass_noise_filter], batch[pass_noise_filter])
#         return out_noise_filter, pass_noise_filter, out_gravnet


def XYZtoXYZPrime(inputs):
    x = inputs[:, 0]
    y = inputs[:, 1]
    z = inputs[:, 2]
    r = torch.norm(inputs[:, 0:3], dim=1, p=2)
    mask_z = z == 0
    z_div = mask_z * torch.sign(z) * 1.0 + (~mask_z) * z * 10
    xprime = (x / z_div).view(-1, 1)
    yprime = (y / z_div).view(-1, 1)
    zprime = (r / 100).view(-1, 1)
    print(z_div)
    return torch.cat((xprime, yprime, zprime), dim=1)
