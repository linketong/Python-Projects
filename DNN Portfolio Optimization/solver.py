import pdb
import sys
import time
import numpy as np
import tensorflow as tf
from tensorflow.keras import Model

TF_DTYPE = tf.float32


class dBSDE(Model):
    def __init__(self, equation, y0, zdx=True, separate_z0=True, lb=None, ub=None):
        super(dBSDE, self).__init__()
        self.bsde = equation
        self.dimw = equation.dim
        self.dimx = equation.dimx
        self.lb = lb
        self.ub = ub

        self.num_time_interval = equation.num_time_interval
        self.total_time = equation.total_time
        self.delta_t = tf.cast(equation.delta_t, dtype=TF_DTYPE)
        self.zdx = zdx
        if zdx:
            self.dimz = self.dimx
        else:
            self.dimz = self.dimw
        self.x0 = tf.cast(equation.x_init, dtype=TF_DTYPE)
        self.y0 = tf.Variable(tf.cast(y0, dtype=TF_DTYPE), name="y0")
        self.separate_z0 = separate_z0
        self.dimz0 = self.dimz
        self.z0 = tf.Variable(tf.constant(0, dtype=TF_DTYPE, shape=[1, self.dimz0]), name="z0")
        self.train_loss = tf.keras.metrics.Mean(name="train_loss")
        self.test_loss = tf.keras.metrics.Mean(name="test_loss")
        self.lr = 0.01
        self._loss_patience_cnt = 0.0
        self._stop_patience_cnt = 0.0
        self.optimizer = tf.keras.optimizers.Adam(learning_rate=self.lr)

        n_nodes = self.dimz + 20
        n_inputs = self.dimx + 2  # [x,y,t]
        dense_layers = [
            tf.keras.layers.Dense(n_nodes, activation=tf.nn.elu) for _ in range(4 + int(np.log(self.dimz)))
        ]
        self.nn_z = tf.keras.Sequential(
            [tf.keras.layers.BatchNormalization(input_shape=(n_inputs,))]
            + dense_layers
            + [tf.keras.layers.Dense(self.dimz)]
        )
        self.hist = {}

    def call(self, inputs, test=False, record=False, log_dir="./logs/"):
        """
        :param inputs: [batch_size, dimx]
        :param record: record history of x, y, z. True for prediction
        :param record_tb: record for tensorboard
        :param log_dir:
        :return: [batch_size, 1]
        """

        """Initialization"""
        dw_sample = tf.reshape(tf.cast(inputs, dtype=TF_DTYPE), [-1, self.dimw, self.num_time_interval])
        batch_size = tf.shape(inputs)[0]
        x = tf.broadcast_to(self.x0, [batch_size, self.dimx])
        y = tf.broadcast_to(self.y0, [batch_size, 1])
        if self.separate_z0:
            z0 = tf.broadcast_to(self.z0, [batch_size, self.dimz0])
        else:
            features = tf.concat([x, tf.zeros([batch_size, 1], dtype=TF_DTYPE), y], axis=1)
            z0 = self.nn_z(features) / self.dimz
            self.z0 = tf.reshape(z0[0, :], [1, tf.shape(z0)[1]])

        dw = dw_sample[:, :, 0]
        current_time = tf.broadcast_to(0.0, [batch_size, 1])
        if test:
            self.hist = {"x": [], "y": [], "z": [], "t": [], "pi": []}
            self.hist["x"].append(tf.reduce_mean(x, axis=0, keepdims=False))
            self.hist["y"].append(tf.reduce_mean(y, keepdims=False))  # y[0]
            self.hist["z"].append(tf.reduce_mean(z0, axis=0, keepdims=False))
            self.hist["t"].append(current_time[0][0])
        y, pi = self.bsde.next_y(current_time, x, y, z0, dw, self.lb, self.ub, zdx=self.zdx)
        if test:
            self.hist["y"].append(tf.reduce_mean(y, keepdims=False))  # y[1]
            self.hist["pi"].append(tf.reduce_mean(pi, axis=0, keepdims=False))  # pi[0]

        """Iterate forward"""
        for t in range(1, self.num_time_interval):
            time = tf.broadcast_to(tf.cast(t, TF_DTYPE) * self.delta_t, [batch_size, 1])
            x = self.bsde.next_x(x, dw)  # x[1]
            features = tf.concat([x, time, y], axis=1)
            z = self.nn_z(features) / self.dimz  # z[1]
            dw = dw_sample[:, :, t]  # [M, dimw] dw[1] adapted to t=2
            y, pi = self.bsde.next_y(
                time, x, y, z, dw, self.lb, self.ub, self.zdx
            )  # y[2] <- t1, x[1], y[1], z[1], dw[1]
            if test:
                self.hist["x"].append(tf.reduce_mean(x, axis=0, keepdims=False))  # x[1]...x[N-1]
                self.hist["y"].append(tf.reduce_mean(y, keepdims=False))  # y[2]...y[N]
                self.hist["z"].append(tf.reduce_mean(z, axis=0, keepdims=False))  # z[1]...z[N-1]
                self.hist["pi"].append(tf.reduce_mean(pi, axis=0, keepdims=False))  # pi[1]...pi[N-1]
                self.hist["t"].append(time[0][0])  # t[1]...t[N-1]
        x = self.bsde.next_x(x, dw)
        return y, x, z

    @tf.function()
    def train_step(self, train_ds):
        with tf.GradientTape() as tape:
            pred_value, _, _ = self.call(train_ds, test=False)
            true_value = self.bsde.g_tf(0, pred_value)
            loss = tf.keras.losses.mean_squared_error(true_value, pred_value)
        gradients = tape.gradient(loss, self.trainable_variables)
        self.optimizer.apply_gradients(zip(gradients, self.trainable_variables))
        self.train_loss.update_state(loss)

    def test_step(self, test_ds):
        pred_value, _, _ = self.call(test_ds, test=True)
        # true_value is given by the g_tf function in bsde equations, i.e. the terminal value.
        true_value = self.bsde.g_tf(0, pred_value)
        loss = tf.keras.losses.mean_squared_error(true_value, pred_value)
        self.test_loss.update_state(loss)

    def custom_fit(self, train_ds, test_ds, epochs):
        start_time = time.time()
        hist_loss = []
        history = {
            "x0": [],
            "T": [],
            "N": [],
            "psi": [],
            "gamma": [],
            "epoch": [],
            "elapsed_time": [],
            "y0": [],
            "z0": [],
            "loss": [],
            "lr": [],
        }
        history["x0"] = self.x0.numpy()
        history["T"] = self.total_time
        history["N"] = self.num_time_interval
        history["psi"] = self.bsde.psi
        history["gamma"] = self.bsde.gamma

        for epoch in range(epochs):
            for x in train_ds:
                self.train_step(x)
            # Test
            for x in test_ds:
                self.test_step(x)
            elapsed_time = time.time() - start_time
            test_loss = self.test_loss.result()
            hist_loss.append(test_loss)

            # Reshape the z0
            if self.zdx and tf.shape(self.z0)[1] == self.dimx:
                sigma_x = self.bsde.sigma_x(tf.expand_dims(self.x0, 0))
                # z0 = tf.squeeze(tf.matmul(tf.expand_dims(self.z0, 0), sigma_x))
                try:
                    z0 = self.bsde.z_T_matmul_sigma_x(self.z0, sigma_x)
                except:
                    z0 = tf.squeeze(tf.matmul(tf.expand_dims(self.z0, 0), sigma_x))
            else:
                z0 = tf.squeeze(self.z0)
            # Print out test result at each epoch
            tf.print(
                "Epoch:",
                epoch + 1,
                "Elapsed time: ",
                elapsed_time,
                "y0: ",
                self.y0,
                "z0: ",
                z0,
                "Test loss: ",
                test_loss,
                "lr: ",
                self.lr,
                output_stream=sys.stdout,
            )
            # Document the training process in history
            history["epoch"].append(epoch + 1)
            history["elapsed_time"].append(elapsed_time)
            history["y0"].append(self.y0.numpy())
            history["z0"].append(z0.numpy())
            history["loss"].append(test_loss.numpy())
            history["lr"].append(self.lr)

            early_stop = self.early_stop(hist_loss, patience=5, min_delta=0.01)
            if early_stop:
                print("Early stopping at plateau")
                break
            self.lr_schedule(hist_loss, patience=3, min_delta=0.05)
            self.train_loss.reset_states()
            self.test_loss.reset_states()
        return history

    def lr_schedule(self, hist_loss, patience=3, min_delta=0.05):
        if len(hist_loss) > 1 and tf.abs(hist_loss[-2] - hist_loss[-1]) / hist_loss[-2] < min_delta:
            self._loss_patience_cnt += 1
        else:
            self._loss_patience_cnt = 0
        if self._loss_patience_cnt > patience:
            self.lr = max(self.lr / 2, 1e-6)

    def early_stop(self, hist_loss, patience=5, min_delta=0.05):
        early_stop = False
        if len(hist_loss) > 1 and tf.abs(hist_loss[-2] - hist_loss[-1]) / hist_loss[-2] < min_delta:
            self._stop_patience_cnt += 1
        else:
            self._stop_patience_cnt = 0
        if self._stop_patience_cnt > patience:
            early_stop = True
        return early_stop
