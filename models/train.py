import os
import time
import i3d
import tensorflow as tf
from pipeline import Pipeline
import numpy as np


NUM_CLASSES = 5
NUM_FRAMES = 64
CROP_SIZE = 224
BATCH_SIZE = 3
STRIDE = NUM_FRAMES
DATA_DIR = "/Users/dewalgupta/Documents/ucsd/291d/activitynet/data"
CLS_DICT_FP = "/Users/dewalgupta/Documents/ucsd/291d/activitynet/Action_Recognition/config/label_map_2.txt"
DROPOUT_KEEP_PROB = 0.5
MAX_ITER = 10
NUM_GPUS = 1

TRAIN_DATA = "/Users/dewalgupta/Documents/ucsd/291d/activitynet/Action_Recognition/config/train.txt"
VAL_DATA = "/Users/dewalgupta/Documents/ucsd/291d/activitynet/Action_Recognition/config/val.txt"

CHECKPOINT_PATHS = {
    'rgb': './checkpoints/rgb_scratch/model.ckpt',
    'rgb_imagenet': './checkpoints/rgb_imagenet/model.ckpt',
}

LR = 0.01
TMPDIR = "./tmp"
LOGDIR = "./log"
THROUGH_PUT_ITER = 5
VAL_ITER = 2
SAVE_ITER = 5
DISPLAY_ITER = 2


# build the model
def inference(rgb_inputs):
    with tf.variable_scope('RGB'):
        rgb_model = i3d.InceptionI3d(
            NUM_CLASSES, spatial_squeeze=True, final_endpoint='Logits')
        rgb_logits, _ = rgb_model(rgb_inputs, is_training=True, dropout_keep_prob=DROPOUT_KEEP_PROB)
    return rgb_logits


# restore the pretrained weights, except for the last layer
def get_pretrained_save_state():
    rgb_variable_map = {}
    for variable in tf.global_variables():
        if variable.name.split('/')[0] == 'RGB':
            if 'Logits' in variable.name:  # skip the last layer
                continue
            rgb_variable_map[variable.name.replace(':0', '')] = variable
    rgb_saver = tf.train.Saver(var_list=rgb_variable_map, reshape=True)
    return rgb_saver


def tower_inference(rgb_inputs, labels):
    rgb_logits = inference(rgb_inputs)
    return tf.reduce_mean(
        tf.nn.sparse_softmax_cross_entropy_with_logits(
            labels=labels, logits=rgb_logits)), rgb_logits


def average_gradients(tower_grads):
    average_grads = []
    for grad_and_vars in zip(*tower_grads):
        # Note that each grad_and_vars looks like the following:
        # ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
        grads = []
        for g, _ in grad_and_vars:
            expanded_g = tf.expand_dims(g, 0)
            grads.append(expanded_g)

        grads_concat = tf.concat(grads, axis=0)
        grads_mean = tf.reduce_mean(grads_concat, axis=0)

        v = grad_and_vars[0][1]
        average_grads.append((grads_mean, v))
    return average_grads


def get_true_counts(tower_logits_labels):
    true_count = 0
    for logits, labels in tower_logits_labels:
        true_count += tf.reduce_sum(
            tf.cast(
                tf.equal(tf.cast(tf.argmax(logits, 1), tf.int32), labels),
                tf.int32
            )
        )
    return true_count


if __name__ == '__main__':
    train_pipeline = Pipeline(TRAIN_DATA)
    val_pipeline = Pipeline(VAL_DATA)

    is_training = tf.placeholder(tf.bool)

    opt = tf.train.GradientDescentOptimizer(LR)

    tower_grads = []
    tower_losses = []
    tower_logits_labels = []

    train_queue = train_pipeline.get_dataset().shuffle(buffer_size=3).batch(2).repeat(MAX_ITER)
    val_queue = val_pipeline.get_dataset().shuffle(buffer_size=3).batch(2).repeat(MAX_ITER)
    # rgbs, labels = train_queue.make_one_shot_iterator().get_next()

    # train_queue = iter(train_pipeline)
    with tf.variable_scope(tf.get_variable_scope()):
        # for i in range(NUM_GPUS):
        #     with tf.name_scope('tower_%d' % i):
        #         rgbs, labels = tf.cond(is_training, lambda: train_queue.get_next(),
        #                                       lambda: val_queue.get_next())
        #         with tf.device('/gpu:%d' % i):
        #             loss, logits = tower_inference(rgbs, labels)
        #             tf.get_variable_scope().reuse_variables()
        #             grads = opt.compute_gradients(loss)
        #             tower_grads.append(grads)
        #             tower_losses.append(loss)
        #             tower_logits_labels.append((logits, labels))

        # rgbs, labels = tf.cond(is_training, lambda: train_queue.get_next(),
        #                                       lambda: val_queue.get_next())
        # rgbs = tf.placeholder(tf.float32,
        #                       shape=(1, NUM_FRAMES, CROP_SIZE, CROP_SIZE, 3))

        # labels = tf.placeholder(tf.int32)
        rgbs, labels = tf.cond(is_training, lambda: train_queue.make_one_shot_iterator().get_next(),
                                      lambda: val_queue.make_one_shot_iterator().get_next())
        # rgbs, labels = train_queue.make_one_shot_iterator().get_next()
        loss, logits = tower_inference(rgbs, labels)
        tf.get_variable_scope().reuse_variables()
        grads = opt.compute_gradients(loss)
        tower_grads.append(grads)
        tower_losses.append(loss)
        tower_logits_labels.append((logits, labels))

    true_count_op = get_true_counts(tower_logits_labels)
    avg_loss = tf.reduce_mean(tower_losses)
    grads = average_gradients(tower_grads)
    train_op = opt.apply_gradients(grads)

    # saver for fine tuning
    if not os.path.exists(TMPDIR):
        os.mkdir(TMPDIR)
    saver = tf.train.Saver(max_to_keep=3)
    ckpt_path = os.path.join(TMPDIR, 'ckpt')
    if not os.path.exists(ckpt_path):
        os.mkdir(ckpt_path)

    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        # batch = [next(train_queue) for _ in range(1)]
        # _f = np.array([item[0] for item in batch])
        # _l = np.array([item[1] for item in batch])
        # print(sess.run([loss], {rgbs: _f, labels: _l, is_training: False}))
        # print(sess.run([rgbs], {is_training: False})[0].shape)

        rgb_def_state = get_pretrained_save_state()
        ckpt = tf.train.get_checkpoint_state(ckpt_path)
        if ckpt and ckpt.model_checkpoint_path:
            tf.logging.info('Restoring from: %s', ckpt.model_checkpoint_path)
            saver.restore(sess, ckpt.all_model_checkpoint_paths[-1])
        else:
            tf.logging.info('No checkpoint file found, restoring pretrained weights...')
            # rgb_def_state.restore(sess, CHECKPOINT_PATHS['rgb_imagenet'])
            rgb_def_state.restore(sess, CHECKPOINT_PATHS['rgb'])
            tf.logging.info('Restore Complete.')

        # prefetch_threads = tf.train.start_queue_runners(sess=sess, coord=coord)

        summary_writer = tf.summary.FileWriter(LOGDIR, sess.graph)
        tf.logging.set_verbosity(tf.logging.INFO)

        it = 0
        last_time = time.time()
        last_step = 0
        val_time = 0
        for epoch in range(MAX_ITER):
            while True:
                print('==== EPOCH : ' + str(epoch) + ' || iter : ' + str(it))
                try:
                    _, loss_val = sess.run([train_op, avg_loss], {is_training: True})

                    if it % DISPLAY_ITER == 0:
                        tf.logging.info('step %d, loss = %.3f', it, loss_val)
                        loss_summ = tf.Summary(value=[
                            tf.Summary.Value(tag="train_loss", simple_value=loss_val)
                        ])
                        summary_writer.add_summary(loss_summ, it)

                    if it % SAVE_ITER == 0 and it > 0:
                        saver.save(sess, os.path.join(ckpt_path, 'model_ckpt'), it)

                    if it % THROUGH_PUT_ITER == 0 and it > 0:
                        duration = time.time() - last_time - val_time
                        steps = it - last_step
                        through_put = steps * NUM_GPUS * BATCH_SIZE / duration
                        tf.logging.info('num examples/sec: %.2f', through_put)
                        through_put_summ = tf.Summary(value=[
                            tf.Summary.Value(tag="through_put", simple_value=through_put)
                        ])
                        summary_writer.add_summary(through_put_summ, it)
                        last_time = time.time()
                        last_step = it
                        val_time = 0

                    it += 1
                except tf.errors.OutOfRangeError as e:
                    break

            ### PERFORM VALIDATION

            val_start = time.time()
            tf.logging.info('validating...')
            true_count = 0
            val_loss = 0
            for i in range(0, len(val_pipeline.videos), NUM_GPUS * BATCH_SIZE):
                c, l = sess.run([true_count_op, avg_loss], {is_training: False})
                true_count += c
                val_loss += l
            # add val accuracy to summary
            acc = true_count / len(val_pipeline.videos)
            tf.logging.info('val accuracy: %.3f', acc)
            acc_summ = tf.Summary(value=[
                tf.Summary.Value(tag="val_acc", simple_value=acc)
            ])
            summary_writer.add_summary(acc_summ, it)
            # add val loss to summary
            val_loss = val_loss / int(len(val_pipeline.videos) / NUM_GPUS / BATCH_SIZE)
            tf.logging.info('val loss: %.3f', val_loss)
            val_loss_summ = tf.Summary(value=[
                tf.Summary.Value(tag="val_loss", simple_value=val_loss)
            ])
            summary_writer.add_summary(val_loss_summ, it)
            val_time = time.time() - val_start
            saver.save(sess, os.path.join(ckpt_path, 'model_ckpt'), it)

        summary_writer.close()
