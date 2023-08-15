import torch
import torch.nn as nn
from torch import Tensor
from torch_scatter import scatter_min, scatter_max, scatter_mean, scatter_add

from src.layers.GravNetConv2 import GravNetConv

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

        super(GravnetModel, self).__init__()
        print("k_gravnet:", k_gravnet)
        k = k_gravnet
        assert activation in ["relu", "tanh", "sigmoid", "elu"]
        acts = {
            "relu": nn.ReLU(),
            "tanh": nn.Tanh(),
            "sigmoid": nn.Sigmoid(),
            "elu": nn.ELU(),
        }
        self.act = acts[activation]

        N_NEIGHBOURS = [16, 40, 16, 40]
        TOTAL_ITERATIONS = len(N_NEIGHBOURS)
        self.return_graphs = False
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.n_gravnet_blocks = TOTAL_ITERATIONS
        self.n_postgn_dense_blocks = n_postgn_dense_blocks
        # self.batchnorm = batchnorm
        # if self.batchnorm:
        self.batchnorm1 = nn.BatchNorm1d(self.input_dim)
        # else:
        #    self.batchnorm1 = nn.Identity()
        self.input = nn.Linear(input_dim, 64, bias=False)
        # self.input.weight.data.copy_(torch.eye(64,input_dim))
        print("clust_space_norm", clust_space_norm)
        assert clust_space_norm in ["twonorm", "tanh", "none"]
        self.clust_space_norm = clust_space_norm

        self.d_shape = 32
        self.gravnet_blocks = nn.ModuleList(
            [
                GravNetBlock(64 if i == 0 else self.d_shape, k=k)
                for i in range(self.n_gravnet_blocks)
            ]
        )

        # Post-GravNet dense layers
        postgn_dense_modules = nn.ModuleList()
        for i in range(self.n_postgn_dense_blocks):
            postgn_dense_modules.extend(
                [
                    nn.Linear(4 * self.d_shape if i == 0 else 64, 64),
                    self.act,  # ,
                ]
            )
        self.postgn_dense = nn.Sequential(*postgn_dense_modules)

        # Output block
        self.output = nn.Sequential(
            nn.Linear(64, 64),
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
        x = self.input(x)
        assert x.device == device

        x_gravnet_per_block = []  # To store intermediate outputs
        graphs = []
        loss_regularizing_neig = 0.0
        loss_ll = 0
        for gravnet_block in self.gravnet_blocks:
            x, graph, loss_regularizing_neig_block, loss_ll_ = gravnet_block(
                g, x, batch
            )
            x_gravnet_per_block.append(x)
            graphs.append(graph)
            loss_regularizing_neig = (
                loss_regularizing_neig_block + loss_regularizing_neig
            )
            loss_ll = loss_ll_ + loss_ll
        x = torch.cat(x_gravnet_per_block, dim=-1)

        # assert x.size() == (x.size(0), 4 * 96)
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
            return x, loss_regularizing_neig, loss_ll

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
        fill_loss_weight=1.0,
        use_average_cc_pos=0.0,
        hgcalloss=False,
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
            hgcal_implementation=hgcalloss,
        )
        if return_resolution:
            return a
        if clust_loss_only:
            loss = a[0] + a[1]  # + 5 * a[14]
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


class GravNetBlock(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int = 96,
        space_dimensions: int = 3,
        propagate_dimensions: int = 22,
        k: int = 40,
        # batchnorm: bool = True
    ):
        super(GravNetBlock, self).__init__()
        self.d_shape = 32
        out_channels = self.d_shape
        propagate_dimensions = self.d_shape * 2
        self.gravnet_layer = GravNetConv(
            self.d_shape, out_channels, space_dimensions, propagate_dimensions, k
        ).jittable()
        self.post_gravnet = nn.Sequential(
            nn.Linear(out_channels + space_dimensions, self.d_shape),
            nn.ELU(),
            nn.Linear(self.d_shape, self.d_shape),
            nn.ELU(),
        )
        self.pre_gravnet = nn.Sequential(
            nn.Linear(in_channels, self.d_shape),
            nn.ELU(),
            nn.Linear(self.d_shape, self.d_shape),
            nn.ELU(),
        )
        self.output = nn.Sequential(nn.Linear(4 * self.d_shape, self.d_shape), nn.ELU())

    def forward(self, g, x: Tensor, batch: Tensor) -> Tensor:
        x = self.pre_gravnet(x)
        x, graph, s_l, loss_regularizing_neig, ll_r = self.gravnet_layer(g, x, batch)
        x = torch.cat((x, s_l), dim=1)
        x = self.post_gravnet(x)
        x = global_exchange(x, batch)
        x = self.output(x)
        return x, graph, loss_regularizing_neig, ll_r
