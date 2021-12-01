import os
from pathlib import Path

import torch
from pydgn.static import *
from pydgn.training.event.dispatcher import EventDispatcher
from pydgn.training.event.handler import EventHandler
from pydgn.training.event.state import State
from pydgn.training.profiler import Profiler
from pydgn.training.util import extend_lists, to_tensor_lists
from torch_geometric.data import Data


def log(msg, logger):
    if logger is not None:
        logger.log(msg)


class SingleGraphSequenceTrainingEngine(TrainingEngine):

    def __init__(self, engine_callback, model, loss, optimizer, scorer=None,
                 scheduler=None, early_stopper=None, gradient_clipping=None,
                 device='cpu', plotter=None, exp_path=None,
                 log_every=1, store_last_checkpoint=False):
        super().__init__(engine_callback, model, loss, optimizer, scorer,
                     scheduler, early_stopper, gradient_clipping, device,
                     plotter, exp_path, log_every, store_last_checkpoint)

    # def _to_data_list(self, x, y):
    #     """
    #     Converts a graphs outputs back to a list of Tensors elements. Useful for incremental architectures.
    #     :param embeddings: a tuple of embeddings: (vertex_output, edge_output, graph_output, other_output). Each of
    #     the elements should be a Tensor.
    #     :param x: big Tensor holding information of different graphs
    #     :param y: target labels Tensor, used to determine whether the task is graph classification or not (to be changed)
    #     :return: a list of PyTorch Geometric Data objects
    #     """
    #     data_list = []
    #
    #     _, counts = torch.unique_consecutive(batch, return_counts=True)
    #     cumulative = torch.cumsum(counts, dim=0)
    #
    #     is_graph_prediction = y.shape[0] == len(cumulative)
    #
    #     y = y.unsqueeze(1) if y.dim() == 1 else y
    #
    #     data_list.append(Data(x=x[:cumulative[0]],
    #                           y=y[0].unsqueeze(0) if is_graph_prediction else y[:, cumulative[0]]))
    #     for i in range(1, len(cumulative)):
    #         g = Data(x=x[cumulative[i - 1]:cumulative[i]],
    #                  y=y[i].unsqueeze(0) if is_graph_prediction else y[cumulative[i - 1]:cumulative[i]])
    #         data_list.append(g)
    #
    #     return data_list
    #
    # def _to_list(self, data_list, embeddings, edge_index, y):
    #     assert isinstance(y, torch.Tensor), "Expecting a tensor as target"
    #
    #     # Crucial: Detach the embeddings to free the computation graph!!
    #     if isinstance(embeddings, tuple):
    #         embeddings = tuple([e.detach().cpu() if e is not None else None for e in embeddings])
    #     elif isinstance(embeddings, torch.Tensor):
    #         embeddings = embeddings.detach().cpu()
    #     else:
    #         raise NotImplementedError('Embeddings not understood, should be torch.Tensor or Tuple of torch.Tensor')
    #
    #     # TODO gestire anche il caso lista di tensori (uno o piu' per istante di tempo)
    #
    #     # Convert embeddings back to a list of torch_geometric Data objects
    #     # Needs also y information to (possibly) use them as a tensor dataset
    #     # CRUCIAL: remember, the loader could have shuffled the data, so we
    #     # need to carry y around as well
    #     if data_list is None:
    #         data_list = []
    #     data_list.extend(self._to_data_list(embeddings, y))
    #     return data_list

    # loop over all data (i.e. computes an epoch)
    def _loop(self, loader, return_node_embeddings=False):

        # Reset epoch state (DO NOT REMOVE)
        self.state.update(epoch_loss=None)
        self.state.update(epoch_score=None)
        self.state.update(loader_iterable=iter(loader))

        # Initialize the model state at time step 0
        t = 0
        state.update(time_step=t)
        model_state = []
        all_y = []

        # This is specific to the single graph sequence scenario
        num_timesteps_per_batch = len(loader)
        state.update(num_timesteps_per_batch=num_timesteps_per_batch)

        # Loop over data
        for id_batch in range(len(loader)):
            self.state.update(id_batch=id_batch)

            # EngineCallback will store fetched data in state.batch_input
            self._dispatch(EventHandler.ON_FETCH_DATA, self.state)

            # Move data to device
            # data is a list of snapshots
            data = self.state.batch_input

            if self.training:
                self._dispatch(EventHandler.ON_TRAINING_BATCH_START, self.state)
            else:
                self._dispatch(EventHandler.ON_EVAL_BATCH_START, self.state)

            # Used to accumulate all losses in the batch (sequence of graphs)
            cumulative_batch_loss = {}

            for snapshot in data:

                _snapshot = snapshot
                snapshot = snapshot.to(self.device)
                edge_index, targets = _snapshot.edge_index, _snapshot.y

                # Helpful when you need to access again the input batch, e.g for some continual learning strategy
                self.state.update(batch_input=_snapshot)
                self.state.update(batch_targets=targets)
                self.state.update(time_step=t)

                num_nodes = _data.x.shape[0]
                self.state.update(batch_num_nodes=num_nodes)

                # Can we do better? we force the user to use y
                assert targets is not None, "You must provide a target value, even a dummy one, for the engine to infer whether this is a node or link or graph problem."
                num_targets = self._num_targets(targets)

                self.state.update(batch_num_targets=num_targets)

                self._dispatch(EventHandler.ON_FORWARD, self.state)

                # EngineCallback will store the outputs in state.batch_outputs
                output = self.state.batch_outputs

                # make sure we have embeddings to store as model state
                assert isinstance(output, tuple) and len(output) > 1

                model_state.append(output[1])
                all_y.append(targets)
                self.state.update(model_state=model_state)

                self._dispatch(EventHandler.ON_COMPUTE_METRICS, self.state)

                for k in state.batch_loss.keys():
                    if k not in cumulative_batch_loss:
                        cumulative_batch_loss[k] = state.batch_loss[k]
                    else:
                        cumulative_batch_loss[k] += state.batch_loss[k]

                # Increment global time counter
                t = t+1
                state.update(time_step=t)

            # Substitute the last time step loss with the cumulative one
            # before calling on_backward
            state.update(batch_loss=cumulative_batch_loss)

            if self.training:
                self._dispatch(EventHandler.ON_BACKWARD, self.state)
                self._dispatch(EventHandler.ON_TRAINING_BATCH_END, self.state)
            else:
                self._dispatch(EventHandler.ON_EVAL_BATCH_END, self.state)

        if return_node_embeddings:
            # Model state will hold a list of tensors, one per time step
            data_list = [Data(x=emb_t.cpu().detach(), y=all_y[i]) for i,emb_t in enumerate(model_state)]
            self.state.update(epoch_data_list=data_list)

    def _train(self, loader):
        self.set_training_mode()

        self._dispatch(EventHandler.ON_TRAINING_EPOCH_START, self.state)

        self._loop(loader, return_node_embeddings=False)

        self._dispatch(EventHandler.ON_TRAINING_EPOCH_END, self.state)

        assert self.state.epoch_loss is not None
        loss, score = self.state.epoch_loss, self.state.epoch_score
        return loss, score, None

    def infer(self, loader, set, return_node_embeddings=False):
        """
        Performs an inference step
        :param loader: the DataLoader
        :set: the type of dataset being used, can be TRAINING, VALIDATION or TEST
        :param return_node_embeddings: whether the model should compute node embeddings or not
        :return: a tuple (loss, score, embeddings)
        """
        self.set_eval_mode()
        self.state.update(set=set)

        self._dispatch(EventHandler.ON_EVAL_EPOCH_START, self.state)

        with torch.no_grad():
            self._loop(loader, return_node_embeddings=return_node_embeddings)  # state has been updated

        self._dispatch(EventHandler.ON_EVAL_EPOCH_END, self.state)

        assert self.state.epoch_loss is not None
        loss, score, data_list = self.state.epoch_loss, self.state.epoch_score, self.state.epoch_data_list

        # Add the main loss we want to return as a special key
        main_loss_name = self.loss_fun.get_main_loss_name()
        loss[MAIN_LOSS] = loss[main_loss_name]

        # Add the main score we want to return as a special key
        # Needed by the experimental evaluation framework
        main_score_name = self.score_fun.get_main_score_name()
        score[MAIN_SCORE] = score[main_score_name]

        return loss, score, data_list

    def train(self, train_loader, validation_loader=None, test_loader=None, max_epochs=100, zero_epoch=False,
              logger=None):
        """
        Trains the model
        :param train_loader: the DataLoader associated with training data
        :param validation_loader: the DataLoader associated with validation data, if any
        :param test_loader:  the DataLoader associated with test data, if any
        :param max_epochs: maximum number of training epochs
        :param zero_epoch: if True, starts again from epoch 0 and resets optimizer and scheduler states.
        :param logger: a log.Logger for logging purposes
        :return: a tuple (train_loss, train_score, train_embeddings, validation_loss, validation_score, validation_embeddings, test_loss, test_score, test_embeddings)
        """

        try:
            # Initialize variables
            val_loss, val_score, val_embeddings_tuple = None, None, None
            test_loss, test_score, test_embeddings_tuple = None, None, None

            self.set_device()

            # Restore training from last checkpoint if possible!
            ckpt_filename = Path(self.exp_path, LAST_CHECKPOINT_FILENAME)
            best_ckpt_filename = Path(self.exp_path, BEST_CHECKPOINT_FILENAME)
            is_early_stopper_ckpt = self.early_stopper.checkpoint if self.early_stopper is not None else False
            # If one changes the options in the config file, the existence of a checkpoint is not enough to
            # decide whether to resume training or not!
            if os.path.exists(ckpt_filename) and (self.store_last_checkpoint or is_early_stopper_ckpt):
                self._restore_checkpoint_and_best_results(ckpt_filename, best_ckpt_filename, zero_epoch)
                log(f'START AGAIN FROM EPOCH {self.state.initial_epoch}', logger)

            self._dispatch(EventHandler.ON_FIT_START, self.state)

            # In case we already have a trained model
            epoch = self.state.initial_epoch

            # Loop over the entire dataset dataset
            for epoch in range(self.state.initial_epoch, max_epochs):
                self.state.update(epoch=epoch)

                self._dispatch(EventHandler.ON_EPOCH_START, self.state)

                self.state.update(set=TRAINING)
                _, _, _ = self._train(train_loader)

                # Compute training output (necessary because on_backward has been called)
                train_loss, train_score, _ = self.infer(train_loader, TRAINING, return_node_embeddings=False)

                # Compute validation output
                if validation_loader is not None:
                    val_loss, val_score, _ = self.infer(validation_loader, VALIDATION, return_node_embeddings=False)

                # Compute test output for visualization purposes only (e.g. to debug an incorrect data split for link prediction)
                if test_loader is not None:
                    test_loss, test_score, _ = self.infer(test_loader, TEST, return_node_embeddings=False)

                # Update state with epoch results
                epoch_results = {
                    LOSSES: {},
                    SCORES: {}
                }

                epoch_results[LOSSES].update({f'{TRAINING}_{k}': v for k, v in train_loss.items()})
                epoch_results[SCORES].update({f'{TRAINING}_{k}': v for k, v in train_score.items()})

                if validation_loader is not None:
                    epoch_results[LOSSES].update({f'{VALIDATION}_{k}': v for k, v in val_loss.items()})
                    epoch_results[SCORES].update({f'{VALIDATION}_{k}': v for k, v in val_score.items()})
                    val_msg_str = f', VL loss: {val_loss} VL score: {val_score}'
                else:
                    val_msg_str = ''

                if test_loader is not None:
                    epoch_results[LOSSES].update({f'{TEST}_{k}': v for k, v in test_loss.items()})
                    epoch_results[SCORES].update({f'{TEST}_{k}': v for k, v in test_score.items()})
                    test_msg_str = f', TE loss: {test_loss} TE score: {test_score}'
                else:
                    test_msg_str = ''

                # Update state with the result of this epoch
                self.state.update(epoch_results=epoch_results)

                # We can apply early stopping here
                self._dispatch(EventHandler.ON_EPOCH_END, self.state)

                # Log performances
                if epoch % self.log_every == 0 or epoch == 1:
                    msg = f'Epoch: {epoch + 1}, TR loss: {train_loss} TR score: {train_score}' + val_msg_str + test_msg_str
                    log(msg, logger)

                if self.state.stop_training:
                    log(f"Stopping at epoch {self.state.epoch}.", logger)
                    break

            # Needed to indicate that training has ended
            self.state.update(stop_training=True)

            # We reached the max # of epochs, get best scores from the early stopper (if any) and restore best model
            if self.early_stopper is not None:
                ber = self.state.best_epoch_results
                # Restore the model according to the best validation score!
                self.model.load_state_dict(ber[MODEL_STATE])
                self.optimizer.load_state_dict(ber[OPTIMIZER_STATE])
            else:
                self.state.update(best_epoch_results={BEST_EPOCH: epoch})
                ber = self.state.best_epoch_results

            # Compute training output
            train_loss, train_score, train_embeddings_tuple = self.infer(train_loader, TRAINING,
                                                                         return_node_embeddings=True)
            # ber[f'{TRAINING}_loss'] = train_loss
            ber[f'{TRAINING}{EMB_TUPLE_SUBSTR}'] = train_embeddings_tuple
            ber.update({f'{TRAINING}_{k}': v for k, v in train_loss.items()})
            ber.update({f'{TRAINING}_{k}': v for k, v in train_score.items()})

            # Compute validation output
            if validation_loader is not None:
                val_loss, val_score, val_embeddings_tuple = self.infer(validation_loader, VALIDATION,
                                                                       return_node_embeddings=True)
                # ber[f'{VALIDATION}_loss'] = val_loss
                ber[f'{VALIDATION}{EMB_TUPLE_SUBSTR}'] = val_embeddings_tuple
                ber.update({f'{TRAINING}_{k}': v for k, v in val_loss.items()})
                ber.update({f'{TRAINING}_{k}': v for k, v in val_score.items()})

            # Compute test output
            if test_loader is not None:
                test_loss, test_score, test_embeddings_tuple = self.infer(test_loader, TEST,
                                                                          return_node_embeddings=True)
                # ber[f'{TEST}_loss'] = test_loss
                ber[f'{TEST}{EMB_TUPLE_SUBSTR}'] = test_embeddings_tuple
                ber.update({f'{TEST}_{k}': v for k, v in test_loss.items()})
                ber.update({f'{TEST}_{k}': v for k, v in test_score.items()})

            self._dispatch(EventHandler.ON_FIT_END, self.state)

            log(f'Chosen is TR loss: {train_loss} TR score: {train_score}, VL loss: {val_loss} VL score: {val_score} '
                f'TE loss: {test_loss} TE score: {test_score}', logger)

            self.state.update(set=None)

        except (KeyboardInterrupt, RuntimeError, FileNotFoundError) as e:
            report = self.profiler.report()
            print(str(e))
            log(str(e), logger)
            log(report, logger)
            raise e
            exit(0)

        # Log profile results
        report = self.profiler.report()
        log(report, logger)

        return train_loss, train_score, train_embeddings_tuple, \
               val_loss, val_score, val_embeddings_tuple, \
               test_loss, test_score, test_embeddings_tuple
