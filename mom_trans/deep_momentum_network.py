import os
import json
import pathlib
import shutil
import copy

# from keras_tuner.tuners.randomsearch import RandomSearch
from abc import ABC, abstractmethod

from tensorflow import keras
import tensorflow as tf
import numpy as np
import pandas as pd
import collections

import keras_tuner as kt

from settings.hp_grid import (
    HP_HIDDEN_LAYER_SIZE,
    HP_DROPOUT_RATE,
    HP_MAX_GRADIENT_NORM,
    HP_LEARNING_RATE,
    HP_MINIBATCH_SIZE,
)

from settings.fixed_params import MODLE_PARAMS

from mom_trans.model_inputs import ModelFeatures
from empyrical import sharpe_ratio

from keras_tuner.distribute import utils as ds_utils


class SharpeLoss(tf.keras.losses.Loss):
    def __init__(self, output_size: int = 1):
        self.output_size = output_size  # in case we have multiple targets => output dim[-1] = output_size * n_quantiles
        super().__init__()

    def call(self, y_true, weights):
        captured_returns = weights * y_true
        mean_returns = tf.reduce_mean(captured_returns)
        return -(
            mean_returns
            / tf.sqrt(
                tf.reduce_mean(tf.square(captured_returns))
                - tf.square(mean_returns)
                + 1e-9
            )
            * tf.sqrt(252.0)
        )


class SharpeValidationLoss(keras.callbacks.Callback):
    # TODO check if weights already exist and pass in best sharpe
    def __init__(
        self,
        inputs,
        returns,
        time_indices,
        num_time,  # including a count for nulls which will be indexed as 0
        early_stopping_patience,
        n_multiprocessing_workers,
        weights_save_location="tmp/checkpoint",
        # verbose=0,
        min_delta=1e-4,
        transaction_costs = None
    ):
        super(keras.callbacks.Callback, self).__init__()
        self.inputs = inputs
        self.returns = returns
        self.time_indices = time_indices
        self.n_multiprocessing_workers = n_multiprocessing_workers
        self.early_stopping_patience = early_stopping_patience
        self.num_time = num_time
        self.min_delta = min_delta

        self.best_sharpe = np.NINF  # since calculating positive Sharpe...
        # self.best_weights = None
        self.weights_save_location = weights_save_location
        # self.verbose = verbose
        self.transaction_costs = transaction_costs

    def set_weights_save_loc(self, weights_save_location):
        self.weights_save_location = weights_save_location

    def on_train_begin(self, logs=None):
        self.patience_counter = 0
        self.stopped_epoch = 0
        self.best_sharpe = np.NINF

    def on_epoch_end(self, epoch, logs=None):
        positions = self.model.predict(
            self.inputs,
            workers=self.n_multiprocessing_workers,
            use_multiprocessing=True,  # , batch_size=1
        )
        
        if self.transaction_costs: 
            diff_position = np.diff(positions, axis=1)
            abs_diff_position = np.abs(diff_position)
            abs_diff_position[np.isnan(abs_diff_position)] = 0.0
            abs_diff_position = np.concatenate((np.expand_dims(np.zeros_like(positions[:, 0, 0]), axis  = (1,2)), abs_diff_position), axis=1)
            
            captured_returns = tf.math.unsorted_segment_mean(
            positions * self.returns - abs_diff_position*self.transaction_costs, self.time_indices, self.num_time
            )[1:]
            # captured_returns = positions * self.returns - abs_diff_position*self.transaction_costs
        else:
            captured_returns = tf.math.unsorted_segment_mean(
                positions * self.returns, self.time_indices, self.num_time
            )[1:]
            # captured_returns = positions*self.returns
        # ignoring null times

        # mean_returns = tf.reduce_mean(captured_returns)
        # sharpe = (
        #     mean_returns
        #     / tf.sqrt(
        #         tf.reduce_mean(tf.square(captured_returns))
        #         - tf.square(mean_returns)
        #         + tf.constant(1e-9, dtype=tf.float64)
        #     )
        #     * tf.sqrt(tf.constant(252.0, dtype=tf.float64))
        # ).numpy()

        # TODO sharpe
        sharpe = (
            tf.reduce_mean(captured_returns)
            / tf.sqrt(
                tf.math.reduce_variance(captured_returns)
                + tf.constant(1e-9, dtype=tf.float64)
            )
            * tf.sqrt(tf.constant(252.0, dtype=tf.float64))
        ).numpy()
        if sharpe > self.best_sharpe + self.min_delta:
            self.best_sharpe = sharpe
            self.patience_counter = 0  # reset the count
            # self.best_weights = self.model.get_weights()
            self.model.save_weights(self.weights_save_location, save_format="h5")
        else:
            # if self.verbose: #TODO
            self.patience_counter += 1
            if self.patience_counter >= self.early_stopping_patience:
                self.stopped_epoch = epoch
                self.model.stop_training = True
                self.model.load_weights(self.weights_save_location)
        logs["sharpe"] = sharpe  # for keras tuner
        print(f"\nval_sharpe {logs['sharpe']}")


# Tuner = RandomSearch
class TunerValidationLoss(kt.tuners.RandomSearch):
    def __init__(
        self,
        hypermodel,
        objective,
        max_trials,
        hp_minibatch_size,
        seed=None,
        hyperparameters=None,
        tune_new_entries=True,
        allow_new_entries=True,
        **kwargs,
    ):
        self.hp_minibatch_size = hp_minibatch_size
        super().__init__(
            hypermodel,
            objective,
            max_trials,
            seed,
            hyperparameters,
            tune_new_entries,
            allow_new_entries,
            **kwargs,
        )

    def run_trial(self, trial, *args, **kwargs):
        kwargs["batch_size"] = trial.hyperparameters.Choice(
            "batch_size", values=self.hp_minibatch_size
        )
        super(TunerValidationLoss, self).run_trial(trial, *args, **kwargs)


class TunerDiversifiedSharpe(kt.tuners.RandomSearch):
    def __init__(
        self,
        hypermodel,
        objective,
        max_trials,
        hp_minibatch_size,
        # directory = None,
        seed=None,
        hyperparameters=None,
        tune_new_entries=True,
        allow_new_entries=True,
        **kwargs,
    ):
        self.hp_minibatch_size = hp_minibatch_size
        super().__init__(
            hypermodel,
            objective,
            max_trials,
            seed,
            hyperparameters,
            tune_new_entries,
            allow_new_entries,
            **kwargs,
        )

    def run_trial(self, trial, *args, **kwargs):
        kwargs["batch_size"] = trial.hyperparameters.Choice(
            "batch_size", values=self.hp_minibatch_size
        )

        original_callbacks = kwargs.pop("callbacks", [])

        for callback in original_callbacks:
            if isinstance(callback, SharpeValidationLoss):
                print(trial.trial_id)
                # tf. mkdir(os.path.join(str(self.project_dir), "trial_" + str(trial.trial_id)))
                callback.set_weights_save_loc(
                    self._get_checkpoint_fname(trial.trial_id , self._reported_step)
                )

        # Run the training process multiple times.
        metrics = collections.defaultdict(list)
        for execution in range(self.executions_per_trial):
            copied_fit_kwargs = copy.copy(kwargs)
            callbacks = self._deepcopy_callbacks(original_callbacks)
            self._configure_tensorboard_dir(callbacks, trial, execution)
            callbacks.append(kt.engine.tuner_utils.TunerCallback(self, trial))
            # Only checkpoint the best epoch across all executions.
            # callbacks.append(model_checkpoint)
            copied_fit_kwargs["callbacks"] = callbacks

            history = self._build_and_fit_model(trial, args, copied_fit_kwargs)
            for metric, epoch_values in history.history.items():
                if self.oracle.objective.direction == "min":
                    best_value = np.min(epoch_values)
                else:
                    best_value = np.max(epoch_values)
                metrics[metric].append(best_value)

        # Average the results across executions and send to the Oracle.
        averaged_metrics = {}
        for metric, execution_values in metrics.items():
            averaged_metrics[metric] = np.mean(execution_values)
        self.oracle.update_trial(
            trial.trial_id, metrics=averaged_metrics, step=self._reported_step
        )


class DeepMomentumNetworkModel(ABC):
    def __init__(self, project_name, hp_directory, hp_minibatch_size, **params):
        params = params.copy()

        self.time_steps = int(params["total_time_steps"])
        self.input_size = int(params["input_size"])
        self.output_size = int(params["output_size"])
        self.n_multiprocessing_workers = int(params["multiprocessing_workers"])
        self.num_epochs = int(params["num_epochs"])
        self.early_stopping_patience = int(params["early_stopping_patience"])
        # self.sliding_window = params["sliding_window"]
        self.random_search_iterations = params["random_search_iterations"]
        self.evaluate_diversified_val_sharpe = params["evaluate_diversified_val_sharpe"]
        self.force_output_sharpe_length = params["force_output_sharpe_length"]
        self.transaction_costs = params["transaction_costs"]

        print("Deep Momentum Network params:")
        for k in params:
            print(f"{k} = {params[k]}")

        # To build model
        def model_builder(hp):
            return self.model_builder(hp)

        if self.evaluate_diversified_val_sharpe:
            self.tuner = TunerDiversifiedSharpe(
                model_builder,
                # objective="val_loss",
                objective=kt.Objective("sharpe", "max"),
                hp_minibatch_size=hp_minibatch_size,
                max_trials=self.random_search_iterations,
                directory=hp_directory,
                project_name=project_name,
            )
        else:
            self.tuner = TunerValidationLoss(
                model_builder,
                objective="val_loss",
                hp_minibatch_size=hp_minibatch_size,
                max_trials=self.random_search_iterations,
                directory=hp_directory,
                project_name=project_name,
            )

    @abstractmethod
    def model_builder(self, hp):
        return

    @staticmethod
    def _index_times(val_time):
        val_time_unique = np.sort(np.unique(val_time))
        if val_time_unique[0]:  # check if ""
            val_time_unique = np.insert(val_time_unique, 0, "")
        mapping = dict(zip(val_time_unique, range(len(val_time_unique))))

        @np.vectorize
        def get_indices(t):
            return mapping[t]

        return get_indices(val_time), len(mapping)

    def hyperparameter_search(self, train_data, valid_data):
        data, labels, active_flags, _, _ = ModelFeatures._unpack(train_data)
        val_data, val_labels, val_flags, _, val_time = ModelFeatures._unpack(valid_data)

        # print('Shape of data, labels and val data: ')
        # print(data.shape, labels.shape, val_data.shape)

        if self.evaluate_diversified_val_sharpe:
            val_time_indices, num_val_time = self._index_times(val_time)
            callbacks = [
                SharpeValidationLoss(
                    val_data,
                    val_labels,
                    val_time_indices,
                    num_val_time,
                    self.early_stopping_patience,
                    self.n_multiprocessing_workers,
                    transaction_costs= self.transaction_costs
                ),
                tf.keras.callbacks.TerminateOnNaN(),
            ]
            # self.model.run_eagerly = True
            self.tuner.search(
                x=data,
                y=labels,
                sample_weight=active_flags,
                epochs=self.num_epochs,
                # batch_size=minibatch_size,
                # covered by Tuner class
                callbacks=callbacks,
                shuffle=True,
                use_multiprocessing=True,
                workers=self.n_multiprocessing_workers,
            )
        else:
            callbacks = [
                tf.keras.callbacks.EarlyStopping(
                    monitor="val_loss",
                    patience=self.early_stopping_patience,
                    min_delta=1e-4,
                ),
                # tf.keras.callbacks.TerminateOnNaN(),
            ]
            # self.model.run_eagerly = True
            self.tuner.search(
                x=data,
                y=labels,
                sample_weight=active_flags,
                epochs=self.num_epochs,
                # batch_size=minibatch_size,
                # covered by Tuner class
                validation_data=(
                    val_data,
                    val_labels,
                    val_flags,
                ),
                callbacks=callbacks,
                shuffle=True,
                use_multiprocessing=True,
                workers=self.n_multiprocessing_workers,
                # validation_batch_size=1,
            )

        best_hp = self.tuner.get_best_hyperparameters(num_trials=1)[0].values
        best_model = self.tuner.get_best_models(num_models=1)[0]
        return best_hp, best_model

    def load_model(
        self,
        hyperparameters,
    ) -> tf.keras.Model:
        hyp = kt.engine.hyperparameters.HyperParameters()
        hyp.values = hyperparameters
        return self.tuner.hypermodel.build(hyp)

    def fit(
        self,
        train_data: np.array,
        valid_data: np.array,
        hyperparameters,
        temp_folder: str,
    ):
        data, labels, active_flags, _, _ = ModelFeatures._unpack(train_data)
        val_data, val_labels, val_flags, _, val_time = ModelFeatures._unpack(valid_data)

        model = self.load_model(hyperparameters)

        if self.evaluate_diversified_val_sharpe:
            val_time_indices, num_val_time = self._index_times(val_time)
            callbacks = [
                SharpeValidationLoss(
                    val_data,
                    val_labels,
                    val_time_indices,
                    num_val_time,
                    self.early_stopping_patience,
                    self.n_multiprocessing_workers,
                    weights_save_location=temp_folder,
                    transaction_costs=self.transaction_costs
                ),
                tf.keras.callbacks.TerminateOnNaN(),
            ]
            # self.model.run_eagerly = True
            model.fit(
                x=data,
                y=labels,
                sample_weight=active_flags,
                epochs=self.num_epochs,
                batch_size=hyperparameters["batch_size"],
                callbacks=callbacks,
                shuffle=True,
                use_multiprocessing=True,
                workers=self.n_multiprocessing_workers,
            )
            model.load_weights(temp_folder)
        else:
            callbacks = [
                tf.keras.callbacks.EarlyStopping(
                    monitor="val_loss",
                    patience=self.early_stopping_patience,
                    min_delta=1e-4,
                    restore_best_weights=True,
                ),
                tf.keras.callbacks.TerminateOnNaN(),
            ]
            # self.model.run_eagerly = True
            model.fit(
                x=data,
                y=labels,
                sample_weight=active_flags,
                epochs=self.num_epochs,
                batch_size=hyperparameters["batch_size"],
                validation_data=(
                    val_data,
                    val_labels,
                    val_flags,
                ),
                callbacks=callbacks,
                shuffle=True,
                use_multiprocessing=True,
                workers=self.n_multiprocessing_workers,
            )
        return model

    def evaluate(self, data, model):
        """Applies evaluation metric to the training data.

        Args:
          data: Dataframe for evaluation
          eval_metric: Evaluation metic to return, based on model definition.

        Returns:
          Computed evaluation loss.
        """

        inputs, outputs, active_entries, _, _ = ModelFeatures._unpack(data)

        if self.evaluate_diversified_val_sharpe:
            _, performance = self.get_positions(data, model, False)
            return performance

        else:
            metric_values = model.evaluate(
                x=inputs,
                y=outputs,
                sample_weight=active_entries,
                workers=32,
                use_multiprocessing=True,
            )

            metrics = pd.Series(metric_values, model.metrics_names)
            return metrics["loss"]

    def get_positions(
        self,
        data,
        model,
        sliding_window=True,
        years_geq=np.iinfo(np.int32).min,
        years_lt=np.iinfo(np.int32).max,
    ):
        inputs, outputs, _, identifier, time = ModelFeatures._unpack(data)
        if sliding_window:
            time = pd.to_datetime(
                time[:, -1, 0].flatten()
            )  # TODO to_datetime maybe not needed
            years = time.map(lambda t: t.year)
            identifier = identifier[:, -1, 0].flatten()
            returns = outputs[:, -1, 0].flatten()
        else:
            time = pd.to_datetime(time.flatten())
            years = time.map(lambda t: t.year)
            identifier = identifier.flatten()
            returns = outputs.flatten()
        mask = (years >= years_geq) & (years < years_lt)

        positions = model.predict(
            inputs,
            workers=self.n_multiprocessing_workers,
            use_multiprocessing=True,  # , batch_size=1
        )
        if sliding_window:
            positions = positions[:, -1, 0].flatten()
        else:
            positions = positions.flatten()

        positions = np.where(positions > 0.6, 1, np.where(positions < 0.4, -1, 0))
        captured_returns = returns * positions
        results = pd.DataFrame(
            {
                "identifier": identifier[mask],
                "time": time[mask],
                "returns": returns[mask],
                "position": positions[mask],
                "captured_returns": captured_returns[mask],
            }
        )

        # don't need to divide sum by n because not storing here
        # mean does not work as well (related to days where no information)
        performance = sharpe_ratio(results.groupby("time")["captured_returns"].sum())

        return results, performance


class LstmDeepMomentumNetworkModel(DeepMomentumNetworkModel):
    def __init__(
        self, project_name, hp_directory, hp_minibatch_size=HP_MINIBATCH_SIZE, **params
    ):
        super().__init__(project_name, hp_directory, hp_minibatch_size, **params)

    def model_builder(self, hp):
        hidden_layer_size = hp.Choice("hidden_layer_size", values=HP_HIDDEN_LAYER_SIZE)
        dropout_rate = hp.Choice("dropout_rate", values=HP_DROPOUT_RATE)
        max_gradient_norm = hp.Choice("max_gradient_norm", values=HP_MAX_GRADIENT_NORM)
        learning_rate = hp.Choice("learning_rate", values=HP_LEARNING_RATE)
        # minibatch_size = hp.Choice("hidden_layer_size", HP_MINIBATCH_SIZE)

        input = keras.Input((self.time_steps, self.input_size))
        lstm = tf.keras.layers.LSTM(
            hidden_layer_size,
            return_sequences=True,
            dropout=dropout_rate,
            stateful=False,
            activation="tanh",
            recurrent_activation="sigmoid",
            recurrent_dropout=0,
            unroll=False,
            use_bias=True,
        )(input)
        dropout = keras.layers.Dropout(dropout_rate)(lstm)

        output = tf.keras.layers.TimeDistributed(
            tf.keras.layers.Dense(
                # self.output_size,
                2,
                activation=tf.nn.tanh,
                kernel_constraint=keras.constraints.max_norm(3),
            )
        )(dropout[..., :, :])

        model = keras.Model(inputs=input, outputs=output)

        adam = keras.optimizers.Adam(lr=learning_rate, clipnorm=max_gradient_norm)

        sharpe_loss = SharpeLoss(self.output_size).call

        model.compile(
            loss=sharpe_loss,
            optimizer=adam,
            sample_weight_mode="temporal",
        )
        return model

class TransformerDeepMomentumNetworkModel(DeepMomentumNetworkModel):
    def __init__(self, project_name, hp_directory, hp_minibatch_size = [512, 1024], **params):
        params = params.copy()
        self.category_counts = params["category_counts"]
        
        super().__init__(project_name, hp_directory, hp_minibatch_size, **params)

    def model_builder(self, hp):    
        # hidden_layer_size = hp.Choice("hidden_layer_size", values=HP_HIDDEN_LAYER_SIZE)
        dropout_rate = hp.Choice("dropout_rate", values=HP_DROPOUT_RATE)
        max_gradient_norm = hp.Choice("max_gradient_norm", values=HP_MAX_GRADIENT_NORM)
        learning_rate = hp.Choice("learning_rate", values=HP_LEARNING_RATE)
        # minibatch_size = hp.Choice("hidden_layer_size", [512, 1024])
        no_heads = hp.Choice("no_heads", values = [2,4])
        no_layers = hp.Choice("no_layers", values = [1,2,3])

        d_q = hp.Choice("dq", values = [8, 16, 32, 64, 128, 256]) # is d_model
        ff_dim = hp.Choice("ff_dim", values = [8, 16, 32, 64])
        # ff_final_dim = hp.Choice("ff_final_dim", values = [1, 2, 4, 8])

        d_k  = d_q // no_heads

        time_steps = self.time_steps
        no_categories = self.category_counts

        inputs = keras.Input(shape = (time_steps, self.input_size))

        x = keras.layers.TimeDistributed(keras.layers.Dense(d_q))(inputs) # output has shape (?, timesteps, d_q)
        x = T2V(d_q)(x) ()

        # x = tf.keras.layers.Dense(d_q)(inputs)
        
        # pos_enc = self.PositionEncoding(d_q)

        ticker_enc, class_enc = self.AssetEmbedding(inputs, d_q)
        x = tf.concat([x, tf.concat([ticker_enc, class_enc], axis = -1)], axis = -1)
        # x = x + pos_enc + ticker_enc + class_enc

        def transformer_encoder(inputs, key_dim, num_heads, ff_dim, dropout=0):
            # Normalization and Attention
            x = tf.keras.layers.LayerNormalization(epsilon=1e-6)(inputs)
            x = tf.keras.layers.MultiHeadAttention(key_dim=key_dim, num_heads=num_heads, dropout=dropout)(x, x, use_causal_mask = True)
            x = tf.keras.layers.Dropout(dropout)(x)
            res = x + inputs

            # Feed Forward Part
            x = tf.keras.layers.LayerNormalization(epsilon=1e-6)(res)
            x = tf.keras.layers.Conv1D(filters=key_dim, kernel_size=1, activation="relu")(x)
            x = tf.keras.layers.Dropout(dropout)(x)
            x = tf.keras.layers.Conv1D(filters=inputs.shape[-1], kernel_size=1)(x)
            
            out = res + x
            x = tf.keras.layers.LayerNormalization(epsilon=1e-6)(out)
            return x

        for _ in range(no_layers):
            x = transformer_encoder(inputs = x, key_dim = d_k, num_heads = no_heads, ff_dim = ff_dim, dropout = dropout_rate)
        
        # x = tf.keras.layers.GlobalAveragePooling1D(data_format="channels_first")(x)

        # x = tf.keras.layers.Dense(ff_final_dim, activation="relu")(x)
        # x = tf.keras.layers.Dropout(dropout_rate)(x)
        
        outputs = tf.keras.layers.TimeDistributed(
            tf.keras.layers.Dense(
                self.output_size, 
                activation = tf.nn.softmax,
                kernel_constraint = keras.constraints.max_norm(3),
                ))(x[..., :, :])  # (batch_size, output_size)

        model = keras.Model(inputs= inputs, outputs=outputs)

        adam = keras.optimizers.Adam(lr=learning_rate, clipnorm=max_gradient_norm)

        sharpe_loss = SharpeLoss(self.output_size).call

        model.compile(
            loss=sharpe_loss,
            optimizer=adam,
            sample_weight_mode="temporal",
        )
        return model
    
    def AssetEmbedding(self, all_inputs, d_model):
        time_steps = self.time_steps
        no_categories = self.category_counts

        num_categorical_variables = len(self.category_counts)
        num_regular_variables = self.input_size - num_categorical_variables

        embedding_sizes = [d_model for _, _ in enumerate(self.category_counts)]

        embeddings = []
        for i in range(num_categorical_variables):

            embedding = keras.Sequential(
                [keras.layers.InputLayer([time_steps]),
                    keras.layers.Embedding(
                        self.category_counts[i],
                        embedding_sizes[i],
                        input_length=time_steps,
                        dtype=tf.float32,
                    ),])
            embeddings.append(embedding)
        categorical_inputs = all_inputs[:, :, num_regular_variables:]

        embedded_inputs = [
                embeddings[i](categorical_inputs[Ellipsis, i])
                for i in range(num_categorical_variables)]

        static_inputs= [embedded_inputs[i][:, :, :] for i in range(num_categorical_variables)]
        # static_inputs = keras.backend.stack(embedded_inputs, axis = 2)
        return static_inputs[0], static_inputs[1] 
    
    
    def get_embeddings(self, all_inputs):
        
        time_steps = self.time_steps

        num_categorical_variables = len(self.category_counts)
        num_regular_variables = self.input_size - num_categorical_variables

        embedding_sizes = [self.hidden_layer_size for _, _ in enumerate(self.category_counts)]

        embeddings = []
        for i in range(num_categorical_variables):
            embedding = keras.Sequential(
                [
                    keras.layers.InputLayer([time_steps]),
                    keras.layers.Embedding(
                        self.category_counts[i],
                        embedding_sizes[i],
                        input_length=time_steps,
                        dtype=tf.float32,),])
            embeddings.append(embedding)

        regular_inputs, categorical_inputs = (
            all_inputs[:, :, :num_regular_variables],
            all_inputs[:, :, num_regular_variables:],
        )

        embedded_inputs = [
            embeddings[i](categorical_inputs[Ellipsis, i])
            for i in range(num_categorical_variables)]

        # Static inputs
        known_categorical_inputs = [
            embedded_inputs[i][:, 0, :]
            for i in range(num_categorical_variables)]


        def convert_real_to_embedding(x):
            """Applies linear transformation for time-varying inputs."""
            return keras.layers.TimeDistributed(
                keras.layers.Dense(self.hidden_layer_size)
            )(x)

        # A priori known inputs
        known_regular_inputs = [
            convert_real_to_embedding(regular_inputs[Ellipsis, i : i + 1])
            for i in range(num_regular_variables)]

        known_combined_layer = keras.backend.stack(
            known_regular_inputs + known_categorical_inputs, axis=-1
        )

        return known_combined_layer
    
    def PositionEncoding(self, output_dim, n=10000):
        # print(type(seq_len), type(output_dim))
        P = np.zeros((self.time_steps, output_dim))
        for k in range(self.time_steps):
            for i in np.arange(int(output_dim/2)):
                denominator = np.power(n, 2*i/output_dim)
                P[k, 2*i] = np.sin(k/denominator)
                P[k, 2*i+1] = np.cos(k/denominator)
        return tf.convert_to_tensor(P, dtype=tf.float32)

class PositionEmbeddingFixedWeights(tf.keras.layers.Layer):
    def __init__(self, sequence_length, vocab_size, output_dim, **kwargs):
        super(PositionEmbeddingFixedWeights, self).__init__(**kwargs)
        word_embedding_matrix = self.get_position_encoding(vocab_size, output_dim)   
        position_embedding_matrix = self.get_position_encoding(sequence_length, output_dim)                                          
        self.word_embedding_layer = keras.layers.Embedding(
            input_dim=vocab_size, output_dim=output_dim,
            weights=[word_embedding_matrix],
            trainable=False
        )
        self.position_embedding_layer = keras.layers.Embedding(
            input_dim=sequence_length, output_dim=output_dim,
            weights=[position_embedding_matrix],
            trainable=False
        )
             
    def get_position_encoding(self, seq_len, d, n=10000):
        P = np.zeros((seq_len, d))
        for k in range(seq_len):
            for i in np.arange(int(d/2)):
                denominator = np.power(n, 2*i/d)
                P[k, 2*i] = np.sin(k/denominator)
                P[k, 2*i+1] = np.cos(k/denominator)
        return P
 
 
    def call(self, inputs):        
        position_indices = tf.range(tf.shape(inputs)[-1])
        embedded_words = self.word_embedding_layer(inputs)
        embedded_indices = self.position_embedding_layer(position_indices)
        return embedded_words + embedded_indices

# class T2V(tf.keras.layers.Layer):
    
#     def __init__(self, output_dim=None, **kwargs):
#         self.output_dim = output_dim
#         super(T2V, self).__init__(**kwargs)
        
#     def build(self, input_shape):
#         self.W = self.add_weight(name='W',
#                       shape=(input_shape[1], self.output_dim),
#                       initializer='uniform',
#                       trainable=True)
#         self.P = self.add_weight(name='P',
#                       shape=(input_shape[1], self.output_dim),
#                       initializer='uniform',
#                       trainable=True)
#         self.w = self.add_weight(name='w',
#                       shape=(input_shape[1], 1),
#                       initializer='uniform',
#                       trainable=True)
#         self.p = self.add_weight(name='p',
#                       shape=(input_shape[1], 1),
#                       initializer='uniform',
#                       trainable=True)
#         super(T2V, self).build(input_shape)
        
#     def call(self, x):
        
#         original = self.w * x + self.p
#         sin_trans = tf.math.sin(tf.multiply(x, self.W) + self.P)

#         return tf.concat([sin_trans, original], -1)
    




# class Time2Vector(tf.keras.layers.Layer):
#   def __init__(self, seq_len, model_dim, **kwargs):
#     super(Time2Vector, self).__init__()
#     self.seq_len = seq_len
#     self.output_dim = model_dim

#   def build(self, input_shape):
#     '''Initialize weights and biases with shape (batch, seq_len)'''
#     self.weights_linear = self.add_weight(name='weight_linear',
#                                 shape=(1, int(self.seq_len), 1),
#                                 initializer='uniform',
#                                 trainable=True)
    
#     self.bias_linear = self.add_weight(name='bias_linear',
#                                 shape=(1, int(self.seq_len),1),
#                                 initializer='uniform',
#                                 trainable=True)
    
#     self.weights_periodic = self.add_weight(name='weight_periodic',
#                                 shape=(1, int(self.seq_len),self.output_dim),
#                                 initializer='uniform',
#                                 trainable=True)

#     self.bias_periodic = self.add_weight(name='bias_periodic',
#                                 shape=(1, int(self.seq_len),self.output_dim),
#                                 initializer='uniform',
#                                 trainable=True)
    
#     super(Time2Vector, self).build(self.seq_len)

#   def call(self, x):
#     '''Calculate linear and periodic time features'''
#     # x = tf.math.reduce_mean(x[:,:,:4], axis=-1)
#     x = tf.expand_dims(x, axis = -1) 
#     time_linear = self.weights_linear * x + self.bias_linear # Linear time feature
#     # time_linear = tf.expand_dims(time_linear, axis=-1) # Add dimension (batch, seq_len, 1)
    
#     time_periodic = tf.math.sin(tf.multiply(x, self.weights_periodic) + self.bias_periodic)
#     # time_periodic = tf.expand_dims(time_periodic, axis=-1) # Add dimension (batch, seq_len, 1)
#     return tf.concat([time_linear, time_periodic], axis=-1) # shape = (batch, seq_len, 2)

#   def get_config(self): # Needed for saving and loading model with custom layer
#     config = super().get_config().copy()
#     config.update({'seq_len': self.seq_len})
#     return config
  
#   def compute_output_shape(self, input_shape): 
#     return (input_shape[0], input_shape[1], self.output_dim)
