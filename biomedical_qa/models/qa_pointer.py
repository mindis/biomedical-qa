import tensorflow as tf

from tensorflow.python.ops.rnn import bidirectional_dynamic_rnn
from tensorflow.contrib.rnn import LSTMBlockCell, GRUBlockCell
from tensorflow.python.ops.rnn_cell import BasicRNNCell

from biomedical_qa.models.attention import dot_co_attention
from biomedical_qa.models.qa_model import ExtractionQAModel
from biomedical_qa.models.beam_search import BeamSearchDecoder
from biomedical_qa.models.rnn_cell import _highway_maxout_network
from biomedical_qa import tfutil
import numpy as np

MAX_ANSWER_LENGTH_HEURISTIC = 10

class QAPointerModel(ExtractionQAModel):

    def __init__(self, size, transfer_model, keep_prob=1.0, transfer_layer_size=None,
                 composition="GRU", devices=None, name="QAPointerModel", depends_on=[],
                 answer_layer_depth=1, answer_layer_poolsize=8,
                 answer_layer_type="dpn"):
        self._composition = composition
        self._device0 = devices[0] if devices is not None else "/cpu:0"
        self._device1 = devices[1 % len(devices)] if devices is not None else "/cpu:0"
        self._device2 = devices[2 % len(devices)] if devices is not None else "/cpu:0"
        self._depends_on = depends_on
        self._transfer_layer_size = size if transfer_layer_size is None else transfer_layer_size
        self._answer_layer_depth = answer_layer_depth
        self._answer_layer_poolsize = answer_layer_poolsize
        self._answer_layer_type = answer_layer_type

        ExtractionQAModel.__init__(self, size, transfer_model, keep_prob, name)

    def _init(self):
        ExtractionQAModel._init(self)
        if self._composition == "GRU":
            cell_constructor = lambda size: GRUBlockCell(size)
        elif self._composition == "RNN":
            cell_constructor = lambda size: BasicRNNCell(size)
        else:
            cell_constructor = lambda size: LSTMBlockCell(size)

        with tf.device(self._device0):
            self._eval = tf.get_variable("is_eval", initializer=False, trainable=False)
            self._set_train = self._eval.initializer
            self._set_eval = self._eval.assign(True)

            self._beam_size = tf.get_variable("beam_size", initializer=1, trainable=False)

            # Fed during Training
            self.correct_start_pointer = - tf.ones([self._batch_size], tf.int64) # Dummy value
            self.answer_partition = tf.cast(tf.range(0, self._batch_size), dtype=tf.int64)

            with tf.control_dependencies(self._depends_on):
                with tf.variable_scope("preprocessing_layer"):

                    self.encoded_question = self._preprocessing_layer(
                        cell_constructor, self.embedded_question,
                        self.question_length, projection_scope="question_proj")

                    # single time attention over question
                    enc_question = tf.slice(self.encoded_question, [0, 0, 0], [-1, -1, self.size])
                    attention_scores = tf.contrib.layers.fully_connected(enc_question, 1,
                                                                         activation_fn=None,
                                                                         weights_initializer=None,
                                                                         biases_initializer=None,
                                                                         scope="attention")
                    attention_scores = tf.squeeze(attention_scores, [2])
                    attention_weights = tf.nn.softmax(attention_scores)
                    attention_weights = tf.expand_dims(attention_weights, 2)
                    self.question_representation = tf.reduce_sum(attention_weights * self.encoded_question, [1])

                    self.encoded_ctxt = self._preprocessing_layer(
                        cell_constructor, self.embedded_context, self.context_length,
                        share_rnn=True, projection_scope="context_proj")

                    # Append NULL word
                    null_word = tf.get_variable(
                        "NULL_WORD", shape=[self.encoded_ctxt.get_shape()[2]],
                        initializer=tf.constant_initializer(0.0))
                    self.encoded_question, self.question_length = self.append_null_word(
                        self.encoded_question, self.question_length, null_word)
                    self.encoded_ctxt, self.context_length = self.append_null_word(
                        self.encoded_ctxt, self.context_length, null_word)

                with tf.variable_scope("match_layer"):
                    self.matched_output = self._match_layer(
                        self.encoded_question, self.encoded_ctxt,
                        cell_constructor)

                with tf.variable_scope("pointer_layer"):
                    if self._answer_layer_type == "dpn":
                        self._start_scores, self._end_scores, self._start_pointer, self._end_pointer = \
                            self._dpn_answer_layer(self.question_representation, self.matched_output,
                                                   cell_constructor)
                    elif self._answer_layer_type == "spn":
                        self._start_scores, self._end_scores, self._start_pointer, self._end_pointer = \
                            self._spn_answer_layer(self.question_representation, self.matched_output)
                    else:
                        raise ValueError("Unknown answer layer type: %s" % self._answer_layer_type)

                self._train_variables = [p for p in tf.trainable_variables() if self.name in p.name]

    def append_null_word(self, tensor, lengths, null_word):

        tiled_null_word = tf.tile(null_word, [self._batch_size])
        reshaped_null_word = tf.reshape(tiled_null_word,
                                        [-1, 1, null_word.get_shape()[0].value])

        rev_tensor = tf.reverse_sequence(tensor, lengths, 1)
        rev_tensor = tf.concat(1, [reshaped_null_word, rev_tensor])
        new_tensor = tf.reverse_sequence(rev_tensor, lengths + 1, 1)

        return new_tensor, lengths + 1

    def _preprocessing_layer(self, cell_constructor, inputs, length, share_rnn=False,
                             projection_scope=None):

        projection_initializer = tf.constant_initializer(np.concatenate([np.eye(self.size), np.eye(self.size)]))
        cell = cell_constructor(self.size)
        with tf.variable_scope("RNN") as vs:
            if share_rnn:
                vs.reuse_variables()
            # Does this do use the same weights for forward & backward? Because
            # same cell instance is passed
            encoded = bidirectional_dynamic_rnn(cell, cell, inputs, length,
                                                dtype=tf.float32, time_major=False)[0]
        encoded = tf.concat(2, encoded)
        projected = tf.contrib.layers.fully_connected(encoded, self.size,
                                                      activation_fn=tf.tanh,
                                                      weights_initializer=projection_initializer,
                                                      scope=projection_scope)

        return projected

    def _match_layer(self, encoded_question, encoded_ctxt, cell_constructor):
        size = self.size

        matched_output = dot_co_attention(encoded_ctxt, self.context_length,
                                          encoded_question, self.question_length)
        # TODO: Append feature if token is in question
        matched_output = tf.nn.bidirectional_dynamic_rnn(cell_constructor(size),
                                                         cell_constructor(size),
                                                         matched_output, sequence_length=self.context_length,
                                                         dtype=tf.float32)[0]
        matched_output = tf.concat(2, matched_output)
        matched_output.set_shape([None, None, 2 * size])

        return matched_output

    def _dpn_answer_layer(self, question_state, context_states, cell_constructor):
        context_states = tf.nn.dropout(context_states, self.keep_prob)
        max_length = tf.cast(tf.reduce_max(self.context_length), tf.int32)

        # dynamic pointing decoder
        controller_cell = cell_constructor(question_state.get_shape()[1].value)
        input_size = context_states.get_shape()[-1].value
        context_states_flat = tf.reshape(context_states, [-1, context_states.get_shape()[-1].value])
        offsets = tf.cast(tf.range(0, self._batch_size), dtype=tf.int64) * (tf.reduce_max(self.context_length))

        cur_state = question_state
        u = tf.zeros(tf.pack([self._batch_size, 2 * input_size]))
        u_e = tf.zeros(tf.pack([self._batch_size, input_size]))
        is_stable = tf.constant(False, tf.bool, [1])
        is_stable = tf.tile(is_stable, tf.pack([tf.cast(self._batch_size, tf.int32)]))
        current_start, current_end = None, None
        start_scores, end_scores = [], []

        for i in range(4):
            if i > 0:
                tf.get_variable_scope().reuse_variables()
            ctr_out, cur_state = controller_cell(u, cur_state)

            with tf.variable_scope("start"):
                # Note: This can theoretically also select the null word
                next_start_scores = _highway_maxout_network(
                    self._answer_layer_depth, self._answer_layer_poolsize,
                    tf.concat(1, [u, ctr_out]), context_states, self.context_length,
                    max_length, self.size)

            next_start = tf.arg_max(next_start_scores, 1)
            u_s = tf.gather(context_states_flat, next_start + offsets)
            u = tf.concat(1, [u_s, u_e])

            with tf.variable_scope("end"):
                next_end_scores = _highway_maxout_network(
                    self._answer_layer_depth, self._answer_layer_poolsize,
                    tf.concat(1, [u, ctr_out]), context_states, self.context_length,
                    max_length, self.size)

            next_end_scores_heuristic = next_end_scores + tfutil.mask_for_lengths(
                next_start, max_length=self.embedder.max_length + 1, mask_right=False)
            next_end = tf.arg_max(next_end_scores_heuristic, 1)

            u_e = tf.gather(context_states_flat, next_end + offsets)
            u = tf.concat(1, [u_s, u_e])

            if i > 0:
                # Once is_stable is true, it'll stay stable
                is_stable = tf.logical_or(is_stable, tf.logical_and(tf.equal(next_start, current_start),
                                                                    tf.equal(next_end, current_end)))
                is_stable_int = tf.cast(is_stable, tf.int64)
                current_start = current_start * is_stable_int + (1 - is_stable_int) * next_start
                current_end = current_end * is_stable_int + (1 - is_stable_int) * next_end
            else:
                current_start = next_start
                current_end = next_end

            start_scores.append(tf.gather(next_start_scores, self.answer_partition))
            end_scores.append(tf.gather(next_end_scores, self.answer_partition))

        end_pointer = tf.gather(current_end, self.answer_partition)
        start_pointer = tf.gather(current_start, self.answer_partition)

        return start_scores, end_scores, start_pointer, end_pointer

    def _spn_answer_layer(self, question_state, context_states):

        # Apply beam search only during evaluation
        beam_size = tf.cond(self._eval,
                            lambda: self._beam_size,
                            lambda: tf.constant(1))

        # During evaluation, we'll do the same for each answer
        answer_partition = tf.cond(self._eval,
                                   lambda: tf.cast(tf.range(tf.shape(question_state)[0]), tf.int64),
                                   lambda: self.answer_partition)

        start_scores, end_scores, starts, ends = self._spn_answer_layer_impl(
            question_state, context_states, answer_partition, beam_size)

        # Expand Evaluation results to match answer_partition
        def expand_if_eval(tensor):
            return tf.cond(self._eval,
                           lambda: tf.gather(tensor, self.answer_partition),
                           lambda: tensor)

        return [expand_if_eval(x) for x in [start_scores, end_scores, starts, ends]]

    def _spn_answer_layer_impl(self, question_state, context_states,
                               answer_partition, beam_size):

        beam_search_decoder = BeamSearchDecoder(beam_size, answer_partition)

        context_states = tf.nn.dropout(context_states, self.keep_prob)
        context_shape = tf.shape(context_states)
        input_size = context_states.get_shape()[-1].value
        context_states_flat = tf.reshape(context_states, [-1, input_size])
        offsets = tf.cast(tf.range(0, self._batch_size), dtype=tf.int64) \
                  * (tf.reduce_max(self.context_length))

        def hmn(input, states, context_lengths):
            # Use context_length - 1 so that the null word is never selected.
            return _highway_maxout_network(self._answer_layer_depth,
                                           self._answer_layer_poolsize,
                                           input,
                                           states,
                                           context_lengths - 1,
                                           context_shape[1],
                                           self.size)

        with tf.variable_scope("start"):
            start_scores = hmn(question_state, context_states,
                               self.context_length)

        predicted_start_pointer = beam_search_decoder.receive_start_scores(start_scores)

        partition = beam_search_decoder.expand_batch(answer_partition)
        question_state = tf.gather(question_state, partition)
        context_states = tf.gather(context_states, partition)
        offsets = tf.gather(offsets, partition)
        context_lengths = tf.gather(self.context_length, partition)

        start_pointer = tf.cond(self._eval,
                                lambda: predicted_start_pointer,
                                lambda: beam_search_decoder.expand_batch(
                                    self.correct_start_pointer))
        u_s = tf.gather(context_states_flat, start_pointer + offsets)

        with tf.variable_scope("end"):
            end_input = tf.concat(1, [u_s, question_state])
            end_scores = hmn(end_input, context_states, context_lengths)

        # Mask end scores for evaluation
        masked_end_scores = end_scores + tfutil.mask_for_lengths(
            start_pointer, mask_right=False, max_length=self.embedder.max_length + 1)
        masked_end_scores = masked_end_scores + tfutil.mask_for_lengths(
            start_pointer + MAX_ANSWER_LENGTH_HEURISTIC + 1,
            max_length=self.embedder.max_length + 1)
        end_scores = tf.cond(self._eval,
                             lambda: masked_end_scores,
                             lambda: end_scores)

        beam_search_decoder.receive_end_scores(end_scores)

        self.top_starts, self.top_ends, self.top_probs = beam_search_decoder.get_top_spans()

        return beam_search_decoder.get_final_prediction()

    def set_eval(self, sess):
        super().set_eval(sess)
        sess.run(self._set_eval)

    def set_train(self, sess):
        super().set_train(sess)
        sess.run(self._set_train)

    def set_beam_size(self, sess, beam_size):
        assign_op = self._beam_size.assign(beam_size)
        sess.run([assign_op])

    @property
    def end_scores(self):
        return self._end_scores

    @property
    def start_scores(self):
        return self._start_scores

    @property
    def predicted_answer_starts(self):
        # for answer extraction models
        return self._start_pointer

    @property
    def predicted_answer_ends(self):
        # for answer extraction models
        return self._end_pointer

    @property
    def train_variables(self):
        return self._train_variables

    def get_config(self):
        config = super().get_config()
        config["type"] = "pointer"
        config["composition"] = self._composition
        config["answer_layer_depth"] = self._answer_layer_depth
        config["answer_layer_poolsize"] = self._answer_layer_poolsize
        config["answer_layer_type"] = self._answer_layer_type
        return config

    @staticmethod
    def create_from_config(config, devices, dropout=0.0, reuse=False):
        """
        :param config: dictionary of parameters for creating an autoreader
        :return:
        """
        # size, max_answer_length, embedder, keep_prob, name="QAModel", reuse=False

        # Set defaults for backword compatibility
        if "answer_layer_depth" not in config:
            config["answer_layer_depth"] = 1
        if "answer_layer_poolsize" not in config:
            config["answer_layer_poolsize"] = 8
        if "answer_layer_type" not in config:
            config["answer_layer_type"] = "dpn"

        from biomedical_qa.models import model_from_config
        transfer_model = model_from_config(config["transfer_model"], devices)
        if transfer_model is None:
            transfer_model = model_from_config(config["transfer_model"], devices)
        qa_model = QAPointerModel(
            config["size"],
            transfer_model=transfer_model,
            name=config["name"],
            composition=config["composition"],
            keep_prob=1.0 - dropout,
            devices=devices,
            answer_layer_depth=config["answer_layer_depth"],
            answer_layer_poolsize=config["answer_layer_poolsize"],
            answer_layer_type=config["answer_layer_type"])

        return qa_model
