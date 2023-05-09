import numpy as np
import awkward as ak
import tqdm
import time
import torch
from collections import defaultdict, Counter
from src.utils.metrics import evaluate_metrics
from src.data.tools import _concat
from src.logger.logger import _logger
import wandb


def _flatten_label(label, mask=None):
    if label.ndim > 1:
        label = label.view(-1)
        if mask is not None:
            label = label[mask.view(-1)]
    # print('label', label.shape, label)
    return label


def _flatten_preds(preds, mask=None, label_axis=1):
    if preds.ndim > 2:
        # assuming axis=1 corresponds to the classes
        preds = preds.transpose(label_axis, -1).contiguous()
        preds = preds.view((-1, preds.shape[-1]))
        if mask is not None:
            preds = preds[mask.view(-1)]
    # print('preds', preds.shape, preds)
    return preds


def _check_scales_centers(iterator):
    regress_items = ["part_theta", "part_phi"]
    centers = np.zeros(2)
    scales = np.zeros(2)
    for ii, item in enumerate(regress_items):
        centers[ii] = iterator._data_config.preprocess_params[item]["center"]
        scales[ii] = iterator._data_config.preprocess_params[item]["scale"]
    return centers, scales


def train_regression(
    model,
    loss_func,
    opt,
    scheduler,
    train_loader,
    dev,
    epoch,
    steps_per_epoch=None,
    grad_scaler=None,
    tb_helper=None,
    logwandb=False,
    local_rank=0,
):
    model.train()

    data_config = train_loader.dataset.config

    total_loss = 0
    num_batches = 0
    sum_abs_err = 0
    sum_sqr_err = 0
    count = 0
    start_time = time.time()
    with tqdm.tqdm(train_loader) as tq:
        for batch_g, y in tq:
            label = y
            num_examples = label.shape[0]
            label = label.to(dev)
            opt.zero_grad()
            with torch.cuda.amp.autocast(enabled=grad_scaler is not None):
                batch_g = batch_g.to(dev)
                model_output = model(batch_g)
                preds = model_output.squeeze()
                loss = loss_func(preds, label)
            if grad_scaler is None:
                loss.backward()
                opt.step()
            else:
                grad_scaler.scale(loss).backward()
                grad_scaler.step(opt)
                grad_scaler.update()

            if scheduler and getattr(scheduler, "_update_per_step", False):
                scheduler.step()

            loss = loss.item()

            num_batches += 1
            count += num_examples
            total_loss += loss
            e = preds - label
            abs_err = e.abs().sum().item()
            sum_abs_err += abs_err
            sqr_err = e.square().sum().item()
            sum_sqr_err += sqr_err

            tq.set_postfix(
                {
                    "lr": "%.2e" % scheduler.get_last_lr()[0]
                    if scheduler
                    else opt.defaults["lr"],
                    "Loss": "%.5f" % loss,
                    "AvgLoss": "%.5f" % (total_loss / num_batches),
                    "MSE": "%.5f" % (sqr_err / num_examples),
                    "AvgMSE": "%.5f" % (sum_sqr_err / count),
                    "MAE": "%.5f" % (abs_err / num_examples),
                    "AvgMAE": "%.5f" % (sum_abs_err / count),
                }
            )

            if tb_helper:
                tb_helper.write_scalars(
                    [
                        ("Loss/train", loss, tb_helper.batch_train_count + num_batches),
                        (
                            "MSE/train",
                            sqr_err / num_examples,
                            tb_helper.batch_train_count + num_batches,
                        ),
                        (
                            "MAE/train",
                            abs_err / num_examples,
                            tb_helper.batch_train_count + num_batches,
                        ),
                    ]
                )
                if tb_helper.custom_fn:
                    with torch.no_grad():
                        tb_helper.custom_fn(
                            model_output=model_output,
                            model=model,
                            epoch=epoch,
                            i_batch=num_batches,
                            mode="train",
                        )

            if logwandb and (num_batches % 50):
                import wandb

                wandb.log({"loss regression": loss})

            if steps_per_epoch is not None and num_batches >= steps_per_epoch:
                break

    time_diff = time.time() - start_time
    _logger.info(
        "Processed %d entries in total (avg. speed %.1f entries/s)"
        % (count, count / time_diff)
    )
    _logger.info(
        "Train AvgLoss: %.5f, AvgMSE: %.5f, AvgMAE: %.5f"
        % (total_loss / num_batches, sum_sqr_err / count, sum_abs_err / count)
    )

    if tb_helper:
        tb_helper.write_scalars(
            [
                ("Loss/train (epoch)", total_loss / num_batches, epoch),
                ("MSE/train (epoch)", sum_sqr_err / count, epoch),
                ("MAE/train (epoch)", sum_abs_err / count, epoch),
            ]
        )
        if tb_helper.custom_fn:
            with torch.no_grad():
                tb_helper.custom_fn(
                    model_output=model_output,
                    model=model,
                    epoch=epoch,
                    i_batch=-1,
                    mode="train",
                )
        # update the batch state
        tb_helper.batch_train_count += num_batches

    if scheduler and not getattr(scheduler, "_update_per_step", False):
        scheduler.step()


def evaluate_regression(
    model,
    test_loader,
    dev,
    epoch,
    for_training=True,
    loss_func=None,
    steps_per_epoch=None,
    eval_metrics=[
        "mean_squared_error",
        "mean_absolute_error",
        "median_absolute_error",
        "mean_gamma_deviance",
    ],
    tb_helper=None,
    logwandb=False,
    energy_weighted=False,
    local_rank=0,
):
    model.eval()

    data_config = test_loader.dataset.config

    center, scales = _check_scales_centers(iter(test_loader.dataset))
    total_loss = 0
    num_batches = 0
    sum_sqr_err = 0
    sum_abs_err = 0
    count = 0
    scores = []
    labels = defaultdict(list)
    observers = defaultdict(list)
    start_time = time.time()
    with torch.no_grad():
        with tqdm.tqdm(test_loader) as tq:
            for batch_g, y in tq:
                batch_g = batch_g.to(dev)
                label = y
                num_examples = label.shape[0]
                label = label.to(dev)
                model_output = model(batch_g)
                print(label.shape, model_output.shape)
                preds = model_output.squeeze().float()

                loss = 0 if loss_func is None else loss_func(preds, label).item()

                num_batches += 1
                count += num_examples
                total_loss += loss * num_examples
                e = preds - label
                abs_err = e.abs().sum().item()
                sum_abs_err += abs_err
                sqr_err = e.square().sum().item()
                sum_sqr_err += sqr_err

                tq.set_postfix(
                    {
                        "Loss": "%.5f" % loss,
                        "AvgLoss": "%.5f" % (total_loss / count),
                        "MSE": "%.5f" % (sqr_err / num_examples),
                        "AvgMSE": "%.5f" % (sum_sqr_err / count),
                        "MAE": "%.5f" % (abs_err / num_examples),
                        "AvgMAE": "%.5f" % (sum_abs_err / count),
                    }
                )

                if tb_helper:
                    if tb_helper.custom_fn:
                        with torch.no_grad():
                            tb_helper.custom_fn(
                                model_output=model_output,
                                model=model,
                                epoch=epoch,
                                i_batch=num_batches,
                                mode="eval" if for_training else "test",
                            )

                if logwandb and (num_batches % 50):
                    wandb.log({"loss val regression": loss})
                    e_nn0 = torch.sum(torch.abs((preds[:, 0] - label[:, 0]))) / len(
                        preds
                    )  # /scales[0]))
                    e_nn1 = torch.sum(torch.abs((preds[:, 1] - label[:, 1]))) / len(
                        preds
                    )  # /scales[1]))
                    e_nn2 = torch.sum(torch.abs((preds[:, 2] - label[:, 2]))) // len(
                        preds
                    )
                    # e_nn3 = torch.sum(torch.abs((preds[:,3] - label[:,3])/scales[3]))
                    # e_nn4 = (preds[:,4] - label[:,4])/scales[4]))
                    # wandb.log({"part_p error ": e_nn0})
                    wandb.log({"part_x error": e_nn0})
                    wandb.log({"part_y error ": e_nn1})
                    wandb.log({"part_z error ": e_nn2})
                    # wandb.log({"part_m error": e_nn3})
                    # wandb.log({"part_pid error": e_nn4})

                if steps_per_epoch is not None and num_batches >= steps_per_epoch:
                    break

    time_diff = time.time() - start_time
    _logger.info(
        "Processed %d entries in total (avg. speed %.1f entries/s)"
        % (count, count / time_diff)
    )

    if tb_helper:
        tb_mode = "eval" if for_training else "test"
        tb_helper.write_scalars(
            [
                ("Loss/%s (epoch)" % tb_mode, total_loss / count, epoch),
                ("MSE/%s (epoch)" % tb_mode, sum_sqr_err / count, epoch),
                ("MAE/%s (epoch)" % tb_mode, sum_abs_err / count, epoch),
            ]
        )
        if tb_helper.custom_fn:
            with torch.no_grad():
                tb_helper.custom_fn(
                    model_output=model_output,
                    model=model,
                    epoch=epoch,
                    i_batch=-1,
                    mode=tb_mode,
                )

    # scores = np.concatenate(scores)
    # labels = {k: _concat(v) for k, v in labels.items()}
    # metric_results = evaluate_metrics(labels[data_config.label_names[0]], scores, eval_metrics=eval_metrics)
    # _logger.info('Evaluation metrics: \n%s', '\n'.join(
    #    ['    - %s: \n%s' % (k, str(v)) for k, v in metric_results.items()]))

    if for_training:
        return total_loss / count
    else:
        # convert 2D labels/scores
        observers = {k: _concat(v) for k, v in observers.items()}
        return total_loss / count, scores, labels, observers
