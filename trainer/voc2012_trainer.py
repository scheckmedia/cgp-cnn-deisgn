import tensorflow as tf
import keras.backend as K
from keras.models import clone_model
from keras.utils import to_categorical, plot_model
from keras.optimizers import SGD, Adam
from keras.callbacks import LearningRateScheduler, EarlyStopping, LambdaCallback
from keras.layers import Dense, Conv2D, Activation
import numpy as np
import operator
from trainer.trainer import ClassifyTrainer
from fcn_utils.activations import softmax_4d
from fcn_utils.callbacks import MeanIoUCallback
from fcn_utils.SegDataGenerator import SegDataGenerator
from fcn_utils.metrics import sparse_accuracy_ignoring_last_label, iou
from fcn_utils.loss_function import softmax_sparse_crossentropy_ignoring_last_label
from fcn_utils.BilinearUpSampling import BilinearUpSampling2D
import os
import csv
from threading import Lock




class Voc2012Trainer(ClassifyTrainer):
    """
    A trainer class for VOC2012 dataset
    """

    def __init__(self, input_shape=(128, 128, 3), target_size=(128, 128),
                 voc_root='', batch_size=32, epochs=50, verbose=0,
                 lr=None, model_path='tmp/', stats_path='tmp/', classes=21):
        """
        Parameters
        ----------
        batch_size: int(32)
            batch_size for cifar10 dataset
        epochs: int(100)
            number of epochs are used for each training process*
        verbose: int(1)ping server

            see keras.model.fit_generator
        """

        ClassifyTrainer.__init__(self, batch_size=batch_size, num_classes=classes, input_shape=input_shape,
                                 epochs=epochs, verbose=verbose)

        self.file_path = os.path.join(voc_root, 'ImageSets', 'Segmentation', 'train.txt')
        self.val_file_path = os.path.join(voc_root, 'ImageSets', 'Segmentation', 'val.txt')
        self.data_dir = os.path.join(voc_root, 'JPEGImages')
        self.label_dir = os.path.join(voc_root, 'SegmentationClass')
        self.data_suffix = '.jpg'
        self.label_suffix = '.png'
        self.target_size = target_size
        self.model_path = model_path
        self.stats_path = stats_path
        self._csv_file = os.path.join(self.stats_path, 'stats.csv')
        self.learning_rates = lr


        self.train_datagen = SegDataGenerator(
                                         horizontal_flip=True,
                                         fill_mode='constant',
                                         label_cval=255)

        self.generator = self.train_datagen.flow_from_directory(
            file_path=self.file_path,
            data_dir=self.data_dir, data_suffix=self.data_suffix,
            label_dir=self.label_dir, label_suffix=self.label_suffix,
            classes=self.num_classes,
            target_size=self.target_size, color_mode='rgb',
            batch_size=self.batch_size, shuffle=True,
            loss_shape=None,
            ignore_label=255,
            # save_to_dir='Images/'
        )
        self.val_generator = SegDataGenerator().flow_from_directory(
            file_path=self.val_file_path,
            data_dir=self.data_dir, data_suffix=self.data_suffix,
            label_dir=self.label_dir, label_suffix=self.label_suffix,
            classes=self.num_classes,
            target_size=self.target_size, color_mode='rgb',
            batch_size=self.batch_size, shuffle=False
        )

        self.file_lock = Lock()

        with open(self._csv_file, 'w') as f:
            header = ['epoch', 'acc', 'loss', 'mean_iou', 'params', 'flops', 'score']
            w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
            w.writerow(header)

    def comp(self, parent, child):
        """
        compares a parent and a child and decides whether a child is beter than a parent
        Parameters
        ----------
        parent: float
            parent score
        child: float
            child score

        Returns
        -------
            True if a child is better than a parent otherwise False
        """
        return operator.lt(parent, child)

    def append_output_layer(self, x):
        """
        appends the last layer (dense, softmax) to an output layer
        Parameters
        ----------
        output: keras.layers.layer
            layer where additional layer should be connected

        Returns
        -------
        keras.layers.layer

        """
        # x = GlobalMaxPooling2D()(x)
        x = Conv2D(self.num_classes, kernel_size=(1, 1), use_bias=False, activation='relu', padding='same')(x)
        x = BilinearUpSampling2D(target_size=self.target_size)(x)

        return x

    def model_improved(self, model, score):
        """

        Parameters
        ----------
        model: keras.models.Model
            model which has the best score for a CGP iteration

        Returns
        -------

        """
        base_path = os.path.abspath(self.model_path)
        if not os.path.exists(base_path):
            os.mkdir(base_path)

        f = os.path.join(self.model_path, 'model_%s_score_%.3f.hdf5' % (model.name, score))
        model.save(f)
        plot_model(model, show_shapes=True,
                   to_file=os.path.join(self.model_path, 'model_%s_score_%.3f.png' % (model.name, score)))
        print("save model %s with score %.5f to file" % (f, score))

    def __call__(self, model, epoch, callbacks=None):
        """
        starts the training of a keras model

        Parameters
        ----------
        model: keras.models.Model
            a keras model which will be trained

        Returns
        -------
        float
            score of the best model

        """

        run_meta = tf.RunMetadata()
        optimizer = Adam(decay=1e-6)
        loss = softmax_sparse_crossentropy_ignoring_last_label
        metrics = [sparse_accuracy_ignoring_last_label]

        callbacks = []
        if self.learning_rates:
            lr_idx = (self.epochs // len(self.learning_rates))
            lr_scheduler = LearningRateScheduler(lambda e: self.learning_rates[e // lr_idx])
            callbacks.append(lr_scheduler)

        callbacks.append(EarlyStopping(monitor='loss', mode='min', patience=50, verbose=1))


        steps = self.generator.nb_sample // self.batch_size
        val_steps = self.val_generator.nb_sample // self.batch_size

        mean = MeanIoUCallback(model, self.val_generator, val_steps, self.num_classes, every_n_epoch=-1, on_end=True)
        callbacks.append(mean)

        _ = clone_model(model, input_tensors=tf.placeholder('float32', shape=(1,) + self.input_shape))
        option_builder = tf.profiler.ProfileOptionBuilder
        profiler = tf.profiler.profile

        opt = option_builder.float_operation()
        opt['output'] = 'none'
        flops = profiler(K.get_session().graph, run_meta=run_meta, options=opt)

        opt = option_builder.trainable_variables_parameter()
        opt['output'] = 'none'
        params = profiler(K.get_session().graph, run_meta=run_meta, options=opt)

        # it seems that maac in tensorflow is counted as two operations
        # I divide the flops by two to get a nearly similar value
        total_flops, total_params = flops.total_float_ops // 2, params.total_parameters
        max_params = 3 * 10**6  # max number of params  # e.g. 3.3M of the MobileNet or 25.56M of ResNet 50
        max_flops = 40 * 10**6    # max number of flops # e.g. 3858M of ResNet 50

        params_factor = (1 - (min(max_params, total_params) / max_params))
        flops_factor = (1 - (min(max_flops, total_flops) / max_flops))

        if params_factor <= 0.0 or flops_factor <= 0.0:
            return self.worst

        model.compile(optimizer=optimizer, loss=loss, metrics=metrics)
        history = model.fit_generator(generator=self.generator, steps_per_epoch=steps, epochs=self.epochs,
                                      workers=4, verbose=self.verbose,
                                      callbacks=callbacks)

        acc = np.max(history.history['sparse_accuracy_ignoring_last_label'])
        loss = np.min(history.history['loss'])
        mean_iou = np.max(mean.mean_ious)

        if np.average(history.history['loss']) >= np.log(10):
            return self.worst

        # equals triangle cross (a x b * c) between params ,flops and acc vector
        # score = params_factor * flops_factor * acc

        score = params_factor * flops_factor * mean_iou

        # score = (params_factor + flops_factor + acc) / 3.0

        print("\n%s" % ("-" * 100))
        print("mean_iou: %.2f ---> params: %d, %.2f ---> flops: %s, %.2f ---> score: %.2f" %
              (mean_iou, total_params, params_factor, "{:,}".format(total_flops), flops_factor, score))
        print("%s\n" % ("-" * 100))

        if self.stats_path:
            if not os.path.exists(self.stats_path):
                os.mkdir(self.stats_path)

            with open(self._csv_file, 'a') as f:
                with self.file_lock:
                    header = [epoch, acc, loss, mean_iou, total_params, total_flops, score]
                    w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
                    w.writerow(header)


        return score
