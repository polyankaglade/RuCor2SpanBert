from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import operator
import random
import math
import json
import threading
import numpy as np
import tensorflow as tf
import tensorflow_hub as hub
import h5py
import os
import sys


import util
import coref_ops
import conll
import metrics
sys.path.append(os.path.abspath('../bert'))
import tokenization
import modeling
import optimization

class CorefModel(object):
  def __init__(self, config):
    self.config = config
    self.max_span_width = config["max_span_width"]
    self.genres = { g:i for i,g in enumerate(config["genres"]) }
    self.eval_data = None # Load eval data lazily.
    self.bert_config = modeling.BertConfig.from_json_file(config["bert_config_file"])
    self.tokenizer = tokenization.FullTokenizer(
                vocab_file=config['vocab_file'], do_lower_case=False)

    input_props = []
    input_props.append((tf.int32, [None, None])) # input_ids.
    input_props.append((tf.int32, [None, None])) # input_mask
    input_props.append((tf.int32, [None])) # Text lengths.
    input_props.append((tf.int32, [None, None])) # Speaker IDs.
    input_props.append((tf.int32, [])) # Genre.
    input_props.append((tf.bool, [])) # Is training.
    input_props.append((tf.int32, [None])) # Gold starts.
    input_props.append((tf.int32, [None])) # Gold ends.
    input_props.append((tf.int32, [None])) # Cluster ids.
    input_props.append((tf.int32, [None])) # Sentence Map

    self.queue_input_tensors = [tf.placeholder(dtype, shape) for dtype, shape in input_props]
    dtypes, shapes = zip(*input_props)
    queue = tf.PaddingFIFOQueue(capacity=10, dtypes=dtypes, shapes=shapes)
    self.enqueue_op = queue.enqueue(self.queue_input_tensors)
    self.input_tensors = queue.dequeue()

    self.predictions, self.loss = self.get_predictions_and_loss(*self.input_tensors)
    #self.predictions = self.get_predictions(*self.input_tensors)
    #self.loss, self.predictions = self.get_loss(*self.input_tensors)
    # bert stuff
    tvars = tf.trainable_variables()
    assignment_map, initialized_variable_names = modeling.get_assignment_map_from_checkpoint(tvars, config['init_checkpoint'])
    tf.train.init_from_checkpoint(config['init_checkpoint'], assignment_map)
    print("**** Trainable Variables ****")
    for var in tvars:
      init_string = ""
      if var.name in initialized_variable_names:
        init_string = ", *INIT_FROM_CKPT*"
      # tf.logging.info("  name = %s, shape = %s%s", var.name, var.shape,
                      # init_string)
      print("  name = %s, shape = %s%s", var.name, var.shape,
                      init_string)

    num_train_steps = int(
                    2802 * self.config['num_epochs'])
    num_warmup_steps = int(num_train_steps * 0.1)
    self.global_step = tf.train.get_or_create_global_step()
    #self.global_step = tf.Variable(0, name="global_step", trainable=False)
    self.train_op = optimization.create_custom_optimizer(tvars,
                      self.loss, self.config['bert_learning_rate'], self.config['task_learning_rate'],
                      num_train_steps, num_warmup_steps, False, self.global_step, freeze=-1)
    # self.train_op = optimization.create_optimizer(tvars,
                      # self.loss, self.config['learning_rate'],
                      # num_train_steps, num_warmup_steps, False, self.global_step)

  def start_enqueue_thread(self, session):
    with open(self.config["train_path"]) as f:
      train_examples = [json.loads(jsonline) for jsonline in f.readlines()]
    def _enqueue_loop():
      while True:
        random.shuffle(train_examples)
        if self.config['single_example']:
          for example in train_examples:
            tensorized_example = self.tensorize_example(example, is_training=True)
            feed_dict = dict(zip(self.queue_input_tensors, tensorized_example))
            session.run(self.enqueue_op, feed_dict=feed_dict)
        else:
          examples = []
          for example in train_examples:
            tensorized = self.tensorize_example(example, is_training=True)
            if type(tensorized) is not list:
              tensorized = [tensorized]
            examples += tensorized
          random.shuffle(examples)
          print('num examples', len(examples))
          for example in examples:
            feed_dict = dict(zip(self.queue_input_tensors, example))
            session.run(self.enqueue_op, feed_dict=feed_dict)
    enqueue_thread = threading.Thread(target=_enqueue_loop)
    enqueue_thread.daemon = True
    enqueue_thread.start()

  def restore(self, session):
    # Don't try to restore unused variables from the TF-Hub ELMo module.
    vars_to_restore = [v for v in tf.global_variables() ]
    saver = tf.train.Saver(vars_to_restore)
    checkpoint_path = os.path.join(self.config["log_dir"], "model.max.ckpt")
    print("Restoring from {}".format(checkpoint_path))
    session.run(tf.global_variables_initializer())
    saver.restore(session, checkpoint_path)


  def tensorize_mentions(self, mentions):
    if len(mentions) > 0:
      starts, ends = zip(*mentions)
    else:
      starts, ends = [], []
    return np.array(starts), np.array(ends)

  def tensorize_span_labels(self, tuples, label_dict):
    if len(tuples) > 0:
      starts, ends, labels = zip(*tuples)
    else:
      starts, ends, labels = [], [], []
    return np.array(starts), np.array(ends), np.array([label_dict[c] for c in labels])

  def get_speaker_dict(self, speakers):
    speaker_dict = {'UNK': 0, '[SPL]': 1}
    for s in speakers:
      if s not in speaker_dict and len(speaker_dict) < self.config['max_num_speakers']:
        speaker_dict[s] = len(speaker_dict)
    return speaker_dict


  def tensorize_example(self, example, is_training):
    clusters = example["clusters"]

    gold_mentions = sorted(tuple(m) for m in util.flatten(clusters))
    gold_mention_map = {m:i for i,m in enumerate(gold_mentions)}
    cluster_ids = np.zeros(len(gold_mentions))
    for cluster_id, cluster in enumerate(clusters):
      for mention in cluster:
        cluster_ids[gold_mention_map[tuple(mention)]] = cluster_id + 1

    sentences = example["sentences"]
    num_words = sum(len(s) for s in sentences)
    speakers = example["speakers"]
    # assert num_words == len(speakers), (num_words, len(speakers))
    speaker_dict = self.get_speaker_dict(util.flatten(speakers))
    sentence_map = example['sentence_map']


    max_sentence_length = 270 #max(len(s) for s in sentences)
    text_len = np.array([len(s) for s in sentences])

    input_ids, input_mask, speaker_ids = [], [], []
    for i, (sentence, speaker) in enumerate(zip(sentences, speakers)):
      sent_input_ids = self.tokenizer.convert_tokens_to_ids(sentence)
      sent_input_mask = [1] * len(sent_input_ids)
      sent_speaker_ids = [speaker_dict.get(s, 0) for s in speaker]
      while len(sent_input_ids) < max_sentence_length:
          sent_input_ids.append(0)
          sent_input_mask.append(0)
          sent_speaker_ids.append(0)
      input_ids.append(sent_input_ids)
      speaker_ids.append(sent_speaker_ids)
      input_mask.append(sent_input_mask)
    input_ids = np.array(input_ids)
    input_mask = np.array(input_mask)
    speaker_ids = np.array(speaker_ids)
    assert num_words == np.sum(input_mask), (num_words, np.sum(input_mask))

    # speaker_dict = { s:i for i,s in enumerate(set(speakers)) }
    # speaker_ids = np.array([speaker_dict[s] for s in speakers])

    doc_key = example["doc_key"]
    genre = self.genres[doc_key[:2]]

    gold_starts, gold_ends = self.tensorize_mentions(gold_mentions)
    example_tensors = (input_ids, input_mask,  text_len, speaker_ids, genre, is_training, gold_starts, gold_ends, cluster_ids, sentence_map)

    if is_training and len(sentences) > self.config["max_training_sentences"]:
      if self.config['single_example']:
        return self.truncate_example(*example_tensors)
      else:
        offsets = range(self.config['max_training_sentences'], len(sentences), self.config['max_training_sentences'])
        tensor_list = [self.truncate_example(*(example_tensors + (offset,))) for offset in offsets]
        return tensor_list
    else:
      return example_tensors

  def truncate_example(self, input_ids, input_mask, text_len, speaker_ids, genre, is_training, gold_starts, gold_ends, cluster_ids, sentence_map, sentence_offset=None):
    # print('fffffffffffffffffffffffff')
    max_training_sentences = self.config["max_training_sentences"]
    num_sentences = input_ids.shape[0]
    assert num_sentences > max_training_sentences

    sentence_offset = random.randint(0, num_sentences - max_training_sentences) if sentence_offset is None else sentence_offset
    word_offset = text_len[:sentence_offset].sum()
    num_words = text_len[sentence_offset:sentence_offset + max_training_sentences].sum()
    input_ids = input_ids[sentence_offset:sentence_offset + max_training_sentences, :]
    input_mask = input_mask[sentence_offset:sentence_offset + max_training_sentences, :]
    speaker_ids = speaker_ids[sentence_offset:sentence_offset + max_training_sentences, :]
    # speaker_ids = speaker_ids[word_offset: word_offset + num_words]
    text_len = text_len[sentence_offset:sentence_offset + max_training_sentences]

    sentence_map = sentence_map[word_offset: word_offset + num_words]
    gold_spans = np.logical_and(gold_ends >= word_offset, gold_starts < word_offset + num_words)
    gold_starts = gold_starts[gold_spans] - word_offset
    gold_ends = gold_ends[gold_spans] - word_offset
    cluster_ids = cluster_ids[gold_spans]

    return input_ids, input_mask, text_len, speaker_ids, genre, is_training,  gold_starts, gold_ends, cluster_ids, sentence_map

  def get_candidate_labels(self, candidate_starts, candidate_ends, labeled_starts, labeled_ends, labels):
    same_start = tf.equal(tf.expand_dims(labeled_starts, 1), tf.expand_dims(candidate_starts, 0)) # [num_labeled, num_candidates]
    same_end = tf.equal(tf.expand_dims(labeled_ends, 1), tf.expand_dims(candidate_ends, 0)) # [num_labeled, num_candidates]
    same_span = tf.logical_and(same_start, same_end) # [num_labeled, num_candidates]
    candidate_labels = tf.matmul(tf.expand_dims(labels, 0), tf.to_int32(same_span)) # [1, num_candidates]
    candidate_labels = tf.squeeze(candidate_labels, 0) # [num_candidates]
    return candidate_labels

  def get_dropout(self, dropout_rate, is_training):
    return 1 - (tf.to_float(is_training) * dropout_rate)

  def coarse_to_fine_pruning(self, top_span_emb, top_span_mention_scores, c):
    k = util.shape(top_span_emb, 0)
    top_span_range = tf.range(k) # [k]
    antecedent_offsets = tf.expand_dims(top_span_range, 1) - tf.expand_dims(top_span_range, 0) # [k, k]
    antecedents_mask = antecedent_offsets >= 1 # [k, k]
    fast_antecedent_scores = tf.expand_dims(top_span_mention_scores, 1) + tf.expand_dims(top_span_mention_scores, 0) # [k, k]
    fast_antecedent_scores += tf.log(tf.to_float(antecedents_mask)) # [k, k]
    fast_antecedent_scores += self.get_fast_antecedent_scores(top_span_emb) # [k, k]
    if self.config['use_prior']:
      antecedent_distance_buckets = self.bucket_distance(antecedent_offsets) # [k, c]
      distance_scores = util.projection(tf.nn.dropout(tf.get_variable("antecedent_distance_emb", [10, self.config["feature_size"]]), self.dropout), 1, initializer=tf.truncated_normal_initializer(stddev=0.02)) #[10, 1]
      antecedent_distance_scores = tf.gather(tf.squeeze(distance_scores, 1), antecedent_distance_buckets) # [k, c]
      fast_antecedent_scores += antecedent_distance_scores

    _, top_antecedents = tf.nn.top_k(fast_antecedent_scores, c, sorted=False) # [k, c]
    top_antecedents_mask = util.batch_gather(antecedents_mask, top_antecedents) # [k, c]
    top_fast_antecedent_scores = util.batch_gather(fast_antecedent_scores, top_antecedents) # [k, c]
    top_antecedent_offsets = util.batch_gather(antecedent_offsets, top_antecedents) # [k, c]
    return top_antecedents, top_antecedents_mask, top_fast_antecedent_scores, top_antecedent_offsets


  def get_predictions_and_loss(self, input_ids, input_mask, text_len, speaker_ids, genre, is_training, gold_starts, gold_ends, cluster_ids, sentence_map):
    model = modeling.BertModel(
      config=self.bert_config,
      is_training=is_training,
      input_ids=input_ids,
      input_mask=input_mask,
      # use_tpu is False
      use_one_hot_embeddings=False)
    all_encoder_layers = model.get_all_encoder_layers()
    # mention_doc = all_encoder_layers[-2]
    # antecedent_doc = all_encoder_layers[-1]
    mention_doc = model.get_sequence_output()
    # context_outputs = model.get_sequence_output()

    self.dropout = self.get_dropout(self.config["dropout_rate"], is_training)

    num_sentences = tf.shape(mention_doc)[0]
    max_sentence_length = tf.shape(mention_doc)[1]
    # context_outputs = tf.nn.dropout(context_outputs, self.lexical_dropout) # [num_sentences, max_sentence_length, emb]
    # text_len_mask = tf.sequence_mask(text_len, maxlen=max_sentence_length) # [num_sentence, max_sentence_length]
    mention_doc = self.flatten_emb_by_sentence(mention_doc, input_mask)
    # antecedent_doc = self.flatten_emb_by_sentence(antecedent_doc, input_mask)
    # antecedent_doc = tf.Print(antecedent_doc, [tf.shape(antecedent_doc), tf.shape(mention_doc)], 'shapes')
    num_words = util.shape(mention_doc, 0)
    antecedent_doc = mention_doc


    # mask out cross-sentence candidates
    # sentence_indices = tf.tile(tf.expand_dims(tf.range(num_sentences), 1), [1, max_sentence_length]) # [num_sentences, max_sentence_length]
    # flattened_sentence_indices = self.flatten_emb_by_sentence(sentence_indices, input_mask) # [num_words]
    flattened_sentence_indices = sentence_map
    # flattened_head_emb = self.flatten_emb_by_sentence(head_emb, text_len_mask) # [num_words]

    candidate_starts = tf.tile(tf.expand_dims(tf.range(num_words), 1), [1, self.max_span_width]) # [num_words, max_span_width]
    candidate_ends = candidate_starts + tf.expand_dims(tf.range(self.max_span_width), 0) # [num_words, max_span_width]
    candidate_start_sentence_indices = tf.gather(flattened_sentence_indices, candidate_starts) # [num_words, max_span_width]
    candidate_end_sentence_indices = tf.gather(flattened_sentence_indices, tf.minimum(candidate_ends, num_words - 1)) # [num_words, max_span_width]
    candidate_mask = tf.logical_and(candidate_ends < num_words, tf.equal(candidate_start_sentence_indices, candidate_end_sentence_indices)) # [num_words, max_span_width]
    flattened_candidate_mask = tf.reshape(candidate_mask, [-1]) # [num_words * max_span_width]
    candidate_starts = tf.boolean_mask(tf.reshape(candidate_starts, [-1]), flattened_candidate_mask) # [num_candidates]
    candidate_ends = tf.boolean_mask(tf.reshape(candidate_ends, [-1]), flattened_candidate_mask) # [num_candidates]
    candidate_sentence_indices = tf.boolean_mask(tf.reshape(candidate_start_sentence_indices, [-1]), flattened_candidate_mask) # [num_candidates]

    candidate_cluster_ids = self.get_candidate_labels(candidate_starts, candidate_ends, gold_starts, gold_ends, cluster_ids) # [num_candidates]

    # compute span embeddings -- don't need this
    candidate_span_emb = self.get_span_emb(mention_doc, mention_doc, candidate_starts, candidate_ends) # [num_candidates, emb]
    # conpute mention scores -- change this
    # context_outputs = tf.Print(context_outputs, [tf.shape(context_outputs)], 'context_outputs')
    candidate_mention_scores =  self.get_mention_scores_old(candidate_span_emb, candidate_starts, candidate_ends)
    #candidate_mention_scores =  self.get_mention_scores_bil(mention_doc, candidate_starts, candidate_ends) # [k, 1]
    # candidate_mention_scores = tf.Print(candidate_mention_scores, [tf.shape(candidate_mention_scores)], 'cand mention scores')
    candidate_mention_scores = tf.squeeze(candidate_mention_scores, 1) # [k]
    #candidate_mention_scores = tf.boolean_mask(candidate_mention_scores, flattened_candidate_mask) # [num_candidates]

    # beam size
    k = tf.minimum(3900, tf.to_int32(tf.floor(tf.to_float(num_words) * self.config["top_span_ratio"])))
    c = tf.minimum(self.config["max_top_antecedents"], k)
    # k = tf.to_int32(tf.floor(tf.to_float(num_words) * self.config["top_span_ratio"]))
    # pull from beam
    top_span_indices = coref_ops.extract_spans(tf.expand_dims(candidate_mention_scores, 0),
                                               tf.expand_dims(candidate_starts, 0),
                                               tf.expand_dims(candidate_ends, 0),
                                               tf.expand_dims(k, 0),
                                               num_words,
                                               True) # [1, k]
    top_span_indices.set_shape([1, None])
    top_span_indices = tf.squeeze(top_span_indices, 0) # [k]

    top_span_starts = tf.gather(candidate_starts, top_span_indices) # [k]
    top_span_ends = tf.gather(candidate_ends, top_span_indices) # [k]
    # don't need this
    top_span_emb = tf.gather(candidate_span_emb, top_span_indices) # [k, emb]
    top_span_cluster_ids = tf.gather(candidate_cluster_ids, top_span_indices) # [k]
    top_span_mention_scores = tf.gather(candidate_mention_scores, top_span_indices) # [k]
    genre_emb = tf.gather(tf.get_variable("genre_embeddings", [len(self.genres), self.config["feature_size"]]), genre) # [emb]
    # speaker_ids - tf.Print(speaker_ids, [tf.shape(speaker_ids)], 'spkid')
    # mention_doc - tf.Print(mention_doc, [tf.shape(mention_doc)], 'smd')
    if self.config['use_metadata']:
      speaker_ids = self.flatten_emb_by_sentence(speaker_ids, input_mask)
      top_span_speaker_ids = tf.gather(speaker_ids, top_span_starts) # [k]i
    else:
        top_span_speaker_ids = None


    # antecedent scores -- change this
    dummy_scores = tf.zeros([k, 1]) # [k, 1]
    # top_span_starts = tf.Print(top_span_starts, [top_span_starts], 'top start')
    # antecedent_score_fn = self.get_span_antecedent_scores if self.config['use_span_reps'] else self.get_antecedent_scores
    # top_antecedent_scores, top_antecedents, top_antecedents_mask = antecedent_score_fn(mention_doc, antecedent_doc, top_span_starts, top_span_ends, top_span_mention_scores)
    top_antecedents, top_antecedents_mask, top_fast_antecedent_scores, top_antecedent_offsets = self.coarse_to_fine_pruning(top_span_emb, top_span_mention_scores, c)

    if self.config['fine_grained']:
      for i in range(self.config["coref_depth"]):
        with tf.variable_scope("coref_layer", reuse=(i > 0)):
          # top_antecedent_emb = tf.gather(top_span_emb, top_antecedents) # [k, c, emb]
          top_antecedent_scores = top_fast_antecedent_scores + self.get_slow_antecedent_scores(top_span_emb, top_antecedents, top_antecedent_emb, top_antecedent_offsets, top_span_speaker_ids, genre_emb) # [k, c]
          top_antecedent_weights = tf.nn.softmax(tf.concat([dummy_scores, top_antecedent_scores], 1)) # [k, c + 1]
          top_antecedent_emb = tf.concat([tf.expand_dims(top_span_emb, 1), top_antecedent_emb], 1) # [k, c + 1, emb]
          attended_span_emb = tf.reduce_sum(tf.expand_dims(top_antecedent_weights, 2) * top_antecedent_emb, 1) # [k, emb]
          with tf.variable_scope("f"):
            f = tf.sigmoid(util.projection(tf.concat([top_span_emb, attended_span_emb], 1), util.shape(top_span_emb, -1))) # [k, emb]
            top_span_emb = f * attended_span_emb + (1 - f) * top_span_emb # [k, emb]
    else:
        top_antecedent_scores = top_fast_antecedent_scores
      

    top_antecedent_scores = tf.concat([dummy_scores, top_antecedent_scores], 1) # [k, c + 1]
    # top_antecedent_scores = tf.Print(top_antecedent_scores, [tf.shape(context_outputs), tf.shape(candidate_ends), top_antecedent_scores, tf.shape(top_antecedent_scores)], 'top_antecedent_scores')

    top_antecedent_cluster_ids = tf.gather(top_span_cluster_ids, top_antecedents) # [k, c]
    # top_antecedents_mask = tf.Print(top_antecedents_mask, [top_antecedents_mask, tf.reduce_sum(tf.to_int32(top_antecedents_mask))], 'top ante amsk')
    top_antecedent_cluster_ids += tf.to_int32(tf.log(tf.to_float(top_antecedents_mask))) # [k, c]
    same_cluster_indicator = tf.equal(top_antecedent_cluster_ids, tf.expand_dims(top_span_cluster_ids, 1)) # [k, c]
    non_dummy_indicator = tf.expand_dims(top_span_cluster_ids > 0, 1) # [k, 1]
    pairwise_labels = tf.logical_and(same_cluster_indicator, non_dummy_indicator) # [k, c]
    dummy_labels = tf.logical_not(tf.reduce_any(pairwise_labels, 1, keepdims=True)) # [k, 1]
    top_antecedent_labels = tf.concat([dummy_labels, pairwise_labels], 1) # [k, c + 1]
    # top_antecedent_labels = tf.Print(top_antecedent_labels, [tf.shape(top_antecedent_labels), tf.reduce_mean(tf.reduce_sum(tf.to_int32(top_antecedent_labels), axis=1))], 'top labels')
    loss = self.softmax_loss(top_antecedent_scores, top_antecedent_labels) # [k]
    loss = tf.reduce_sum(loss) # []

    return [candidate_starts, candidate_ends, candidate_mention_scores, top_span_starts, top_span_ends, top_antecedents, top_antecedent_scores], loss

  def get_segment_pairs(self, mention_segments, antecedent_segments, num_segs):
    # make unique
    men_ante_1d = mention_segments * num_segs + antecedent_segments
    unique_men_ante_1d, to_unique_idx = tf.unique(men_ante_1d)
    unique_mention_segments = unique_men_ante_1d // num_segs
    unique_antecedent_segments = unique_men_ante_1d % num_segs # [kc]
    # get tokens
    mention_segment_tokens = tf.gather(tokens, unique_mention_segments)
    antecedent_segment_tokens = tf.gather(tokens, unique_antecedent_segments)
    return mention_segment_tokens, antecedent_segment_tokens, to_unique_idx

  def get_paired_segments(self, mention_segment_tokens, antecedent_segment_tokens):
    pairs = tf.concat((mention_segment_tokens, antecedent_segment_tokens[:, 1:]), axis=1)
    return pairs

  def get_segmented_idxs(self, to_unique_idxs, word_seg_idxs, idxs):
    seg_idx = tf.gather(word_seg_idxs, idxs)
    return to_unique_idxs * num_segs + seg_idx

  def get_span_reps(segment_pair_reps, top_starts, top_ends, top_antecedents):
    segmented_starts, segmented_ends = None, None # [k]
    segmented_antecedent_starts, segmented_antecedent_ends = tf.gather(segmented_starts, top_antecedents), tf.gather(segmented_ends, top_antecedents)
    mention_token_reps, antecedent_token_reps = segment_pair_reps[:, :256], segment_pair_reps[:, 256:]


  def get_cross_segment_embeddings(self, tokens, mask, top_starts, top_ends,top_antecedents):
    k, c = util.shape(top_starts), util.shape(top_antecedents)[1]
    num_segs, seg_len = util.shape(tokens)
    starts_tiled = tf.tile(top_starts, [c])
    word_segments = tf.tile(tf.expand_dims(tf.range(0, num_segs), 1), [1, seg_len])
    word_seg_idxs = tf.tile(tf.expand_dims(tf.range(0, seg_len), 0), [num_segs, 1])
    mention_segments = tf.gather(word_segments, starts_tiled)
    antecedent_segments = tf.gather(word_segments, antecedent_starts)
    mention_segment_tokens, antecedent_segment_tokens = self.get_segment_pairs(mention_segments, antecedent_segments, num_segs)
    paired_segments, paired_mask = self.get_paired_segments()
    model = modeling.BertModel(
      config=self.bert_config,
      is_training=is_training,
      input_ids=paired_segments,
      input_mask=paired_masks,
      # use_tpu is False
      use_one_hot_embeddings=False)
    segment_pair_reps = model.get_sequence_output()
    mention_token_reps, antecedent_token_reps = None, None 
    mention_span_reps, antecedent_span_reps = None, None #[k, emb], [k, c, emb]
    return mention_span_reps, antecedent_span_reps

    pass


  def get_span_emb(self, head_emb, context_outputs, span_starts, span_ends):
    span_emb_list = []

    span_start_emb = tf.gather(context_outputs, span_starts) # [k, emb]
    span_emb_list.append(span_start_emb)

    span_end_emb = tf.gather(context_outputs, span_ends) # [k, emb]
    span_emb_list.append(span_end_emb)

    span_width = 1 + span_ends - span_starts # [k]

    if self.config["use_features"]:
      span_width_index = span_width - 1 # [k]
      span_width_emb = tf.gather(tf.get_variable("span_width_embeddings", [self.config["max_span_width"], self.config["feature_size"]]), span_width_index) # [k, emb]
      span_width_emb = tf.nn.dropout(span_width_emb, self.dropout)
      span_emb_list.append(span_width_emb)

    if self.config["model_heads"]:
      mention_word_scores = self.get_masked_mention_word_scores(context_outputs, span_starts, span_ends)
      head_attn_reps = tf.matmul(mention_word_scores, context_outputs) # [K, T]
      span_emb_list.append(head_attn_reps)
      # span_indices = tf.expand_dims(tf.range(self.config["max_span_width"]), 0) + tf.expand_dims(span_starts, 1) # [k, max_span_width]
      # span_indices = tf.minimum(util.shape(context_outputs, 0) - 1, span_indices) # [k, max_span_width]
      # span_text_emb = tf.gather(head_emb, span_indices) # [k, max_span_width, emb]
      # with tf.variable_scope("head_scores"):
        # self.head_scores = util.projection(context_outputs, 1) # [num_words, 1]
      # span_head_scores = tf.gather(self.head_scores, span_indices) # [k, max_span_width, 1]
      # span_mask = tf.expand_dims(tf.sequence_mask(span_width, self.config["max_span_width"], dtype=tf.float32), 2) # [k, max_span_width, 1]
      # span_head_scores += tf.log(span_mask) # [k, max_span_width, 1]
      # span_attention = tf.nn.softmax(span_head_scores, 1) # [k, max_span_width, 1]
      # span_head_emb = tf.reduce_sum(span_attention * span_text_emb, 1) # [k, emb]
      # span_emb_list.append(span_head_emb)

    span_emb = tf.concat(span_emb_list, 1) # [k, emb]
    return span_emb # [k, emb]


  def get_mention_scores_old(self, span_emb, span_starts, span_ends):
      with tf.variable_scope("mention_scores"):
        span_scores = util.ffnn(span_emb, self.config["ffnn_depth"], self.config["ffnn_size"], 1, self.dropout) # [k, 1]
      if self.config['use_prior']:
        span_width_emb = tf.get_variable("span_width_prior_embeddings", [self.config["max_span_width"], self.config["feature_size"]]) # [W, emb]
        span_width_index = span_ends - span_starts # [NC]
        with tf.variable_scope("width_scores"):
          width_scores =  util.ffnn(span_width_emb, self.config["ffnn_depth"], self.config["ffnn_size"], 1, self.dropout) # [W, 1]
        width_scores = tf.gather(width_scores, span_width_index)
        span_scores += width_scores
      return span_scores

  def get_mention_scores(self, encoded_doc, span_starts, span_ends):
      num_words = util.shape(encoded_doc, 0) # T
      # span_starts = tf.Print(span_starts, [tf.shape(span_starts)], 'span_starts')
      with tf.variable_scope("start_scores"):
        start_scores =  tf.squeeze(util.ffnn(encoded_doc, self.config["ffnn_depth"], self.config["ffnn_size"], 1, self.dropout, hidden_initializer=tf.truncated_normal_initializer(stddev=0.02), output_weights_initializer=tf.truncated_normal_initializer(stddev=0.02)), 1) # [T]
        #start_scores =  tf.squeeze(util.linear(encoded_doc, 1), 1) # [T]
      with tf.variable_scope("end_scores"):
        end_scores =  tf.squeeze(util.ffnn(encoded_doc, self.config["ffnn_depth"], self.config["ffnn_size"], 1, self.dropout, hidden_initializer=tf.truncated_normal_initializer(stddev=0.02), output_weights_initializer=tf.truncated_normal_initializer(stddev=0.02) ), 1) # [T]
        #end_scores =  tf.squeeze(util.linear(encoded_doc, 1), 1) # [T]
      start_end_scores = tf.tile(tf.expand_dims(start_scores, 1), [1, num_words]) + tf.tile(tf.expand_dims(end_scores, 0), [num_words, 1]) # [T, T]
      # start_end_scores = tf.Print(start_end_scores, [tf.shape(start_end_scores)], 'start_end')
      #span_start_doc_scores = tf.gather(start_end_scores, tf.tile(tf.expand_dims(span_starts, 1), [1, num_words])) #[NC, T]
      #span_scores = tf.gather(span_start_doc_scores, tf.expand_dims(span_ends, 1), axis=1) # [NC, 1]`
      span_start_doc_scores = tf.gather(start_end_scores, span_starts) #[NC, T]
      # span_start_doc_scores = tf.Print(span_start_doc_scores, [tf.shape(span_start_doc_scores)], 'start_start_doc')
      span_scores = util.batch_gather(span_start_doc_scores, tf.expand_dims(span_ends, 1)) # [NC, 1]`
      # span_scores = tf.expand_dims(tf.gather(start_scores, span_starts) + tf.gather(end_scores, span_ends), 1)
      span_width = 1 + span_ends - span_starts # [NC]
      if self.config["use_features"]:
        span_width_index = span_width - 1 # [k]
        #span_width_emb = tf.gather(tf.get_variable("span_width_embeddings", [self.config["max_span_width"], self.config["feature_size"]]), span_width_index) # [k, emb]
        span_width_emb = tf.get_variable("span_width_embeddings", [self.config["max_span_width"], self.config["feature_size"]]) # [W, emb]
        span_width_emb = tf.nn.dropout(span_width_emb, self.dropout)
        with tf.variable_scope("width_scores"):
          width_scores =  util.ffnn(span_width_emb, self.config["ffnn_depth"], self.config["ffnn_size"], 1, self.dropout) # [W, 1]
        width_scores = tf.gather(width_scores, span_width_index)
        span_scores += width_scores
      # span_scores = tf.Print(span_scores, [tf.shape(span_scores)], 'span_scores')
      return span_scores

  def get_width_scores(self, doc, starts, ends):
    distance = ends - starts
    span_start_emb = tf.gather(doc, starts)
    hidden = util.shape(doc, 1)
    with tf.variable_scope('span_width'):
      span_width_emb = tf.gather(tf.get_variable("start_width_embeddings", [self.config["max_span_width"], hidden], initializer=tf.truncated_normal_initializer(stddev=0.02)), distance) # [W, emb]
    #scores = tf.expand_dims(tf.reduce_sum(span_start_emb * span_width_emb, axis=1), 1)
    scores = tf.reduce_sum(span_start_emb * span_width_emb, axis=1)
    return scores

    

  def get_mention_scores_bil(self, encoded_doc, span_starts, span_ends):
      num_words = util.shape(encoded_doc, 0) # T
      # span_starts = tf.Print(span_starts, [tf.shape(span_starts)], 'span_starts')
      encoded_doc_start = encoded_doc_end = encoded_doc
      dropout = self.dropout
      with tf.variable_scope("start_scores"):
        start_scores =  tf.squeeze(util.projection(encoded_doc, 1, initializer=tf.truncated_normal_initializer(stddev=0.02)), 1) # [T]
        # start_scores =  tf.squeeze(util.ffnn(encoded_doc, self.config["ffnn_depth"], self.config["ffnn_size"], 1, dropout, hidden_initializer=tf.truncated_normal_initializer(stddev=0.02), output_weights_initializer=tf.truncated_normal_initializer(stddev=0.02)), 1) # [T]
      with tf.variable_scope("end_scores"):
        # end_scores =  tf.squeeze(util.ffnn(encoded_doc, self.config["ffnn_depth"], self.config["ffnn_size"], 1, dropout, hidden_initializer=tf.truncated_normal_initializer(stddev=0.02), output_weights_initializer=tf.truncated_normal_initializer(stddev=0.02) ), 1) # [T]
        end_scores =  tf.squeeze(util.projection(encoded_doc, 1, initializer=tf.truncated_normal_initializer(stddev=0.02)), 1) # [T]
      start_end_scores = tf.expand_dims(start_scores, 1) + tf.expand_dims(end_scores, 0)
      if self.config['mention_bilinear']:
        with tf.variable_scope("mention_bilinear"):
          start_end_scores += self.get_bilinear_scores_xWy(encoded_doc_start, 'W_men_se', encoded_doc_end) 
      span_start_doc_scores = tf.gather(start_end_scores, span_starts) #[NC, T]
      # span_start_doc_scores = tf.Print(span_start_doc_scores, [tf.shape(span_start_doc_scores)], 'start_start_doc')
      span_scores = util.batch_gather(span_start_doc_scores, tf.expand_dims(span_ends, 1)) # [NC, 1]
      if self.config["use_features"]:
        span_width_index = span_ends - span_starts # [k]
        #span_width_emb = tf.gather(tf.get_variable("span_width_embeddings", [self.config["max_span_width"], self.config["feature_size"]]), span_width_index) # [k, emb]
        with tf.variable_scope('span_width'):
          span_width_emb = tf.get_variable("span_width_embeddings", [self.config["max_span_width"], self.config["feature_size"]], initializer=tf.truncated_normal_initializer(stddev=0.02)) # [W, emb]
        span_width_emb = tf.nn.dropout(span_width_emb, self.dropout)
        with tf.variable_scope("width_scores"):
          # width_scores =  util.ffnn(span_width_emb, self.config["ffnn_depth"], self.config["ffnn_size"], 1, self.dropout) # [W, 1]
          width_scores =  util.projection(span_width_emb,  1, initializer=tf.truncated_normal_initializer(stddev=0.02)) # [W, 1]
        width_scores = tf.gather(width_scores, span_width_index)
        span_scores += width_scores
        #span_scores += self.get_width_scores(encoded_doc, span_starts, span_ends)
      return span_scores

  def get_masked_mention_word_scores(self, encoded_doc, span_starts, span_ends):
      num_words = util.shape(encoded_doc, 0) # T
      num_c = util.shape(span_starts, 0) # NC
      doc_range = tf.tile(tf.expand_dims(tf.range(0, num_words), 0), [num_c, 1]) # [K, T]
      mention_mask = tf.logical_and(doc_range >= tf.expand_dims(span_starts, 1), doc_range <= tf.expand_dims(span_ends, 1)) #[K, T]
      with tf.variable_scope("mention_word_attn"):
        word_attn = tf.squeeze(util.projection(encoded_doc, 1, initializer=tf.truncated_normal_initializer(stddev=0.02)), 1)
      mention_word_attn = tf.nn.softmax(tf.log(tf.to_float(mention_mask)) + tf.expand_dims(word_attn, 0))
      # mention_word_attn = tf.nn.softmax(tf.to_float(mention_mask))
      return mention_word_attn

  def get_span_antecedent_scores(self, encoded_doc, span_starts, span_ends, candidate_mention_scores):
    num_words = util.shape(encoded_doc, 0) # T
    num_c = util.shape(span_starts, 0) # NC
    antecedents_mask, antecedents, top_antecedent_offsets = self.get_antecedent_mask(num_c)
    start_reps = tf.gather(encoded_doc, span_starts)
    end_reps = tf.gather(encoded_doc, span_ends)
    span_reps = tf.concat((start_reps, end_reps), -1)
    if self.config['use_head_attn']:
      mention_word_scores = self.get_masked_mention_word_scores(encoded_doc, span_starts, span_ends)
      head_attn_reps = tf.matmul(mention_word_scores, encoded_doc) # [K, T]
      span_reps = tf.concat((span_reps, head_attn_reps), -1)
    if self.config['use_features']:
      span_width_index = span_ends - span_starts # [NC]
      with tf.variable_scope('span_width', reuse=True):
        span_width_emb = tf.get_variable("span_width_embeddings", [self.config["max_span_width"], self.config["feature_size"]], initializer=tf.truncated_normal_initializer(stddev=0.02)) # [W, emb]
      width_reps = tf.gather(span_width_emb, span_width_index)
      span_reps = tf.concat((span_reps, width_reps), -1)

    with tf.variable_scope("span_rep_ffnn"):
      span_reps =  util.ffnn(span_reps, self.config["ffnn_depth"], self.config["ffnn_size"], self.config["ffnn_size"], self.dropout, hidden_initializer=tf.truncated_normal_initializer(stddev=0.02), output_weights_initializer=tf.truncated_normal_initializer(stddev=0.02)) # [T]
    with tf.variable_scope("span_rep_score"):
      span_rep_scores =  tf.squeeze(util.projection(span_reps, 1, initializer=tf.truncated_normal_initializer(stddev=0.02)), 1) # [T]
    top_antecedent_scores = 2 * self.get_bilinear_scores_xWy(span_reps, 'span_pair_scores', span_reps)
    top_antecedent_scores  += tf.log(tf.to_float(antecedents_mask)) # [k, k]
    top_antecedent_scores +=  tf.expand_dims(candidate_mention_scores, 1) + tf.expand_dims(candidate_mention_scores, 0) # [k, k]
    top_antecedent_scores +=  tf.expand_dims(span_rep_scores, 1) + tf.expand_dims(span_rep_scores, 0) # [k, k]
    top_antecedent_scores  += tf.log(tf.to_float(antecedents_mask)) # [k, k]
    c = num_c #tf.minimum(500, num_c)
    _, top_antecedents = tf.nn.top_k(top_antecedent_scores, c, sorted=False) # [nc, c]

    k = util.shape(top_antecedent_scores, 0)
    top_antecedents_mask = util.batch_gather(antecedents_mask, top_antecedents) # [nc, c]
    top_antecedent_scores = util.batch_gather(top_antecedent_scores, top_antecedents) # [k, c]
    top_antecedent_offsets = util.batch_gather(top_antecedent_offsets, top_antecedents) # [k, c]
    if self.config['use_features']:
      antecedent_distance_buckets = self.bucket_distance(top_antecedent_offsets) # [k, c]
      distance_scores = util.projection(tf.nn.dropout(tf.get_variable("antecedent_distance_emb", [10, self.config["feature_size"]]), self.dropout), 1, initializer=tf.truncated_normal_initializer(stddev=0.02)) #[10, 1]
      antecedent_distance_scores = tf.gather(tf.squeeze(distance_scores, 1), antecedent_distance_buckets) # [k, c]
      top_antecedent_scores += antecedent_distance_scores
    return top_antecedent_scores, top_antecedents, top_antecedents_mask



  def get_antecedent_scores(self, mention_doc, antecedent_doc, span_starts, span_ends, candidate_mention_scores):
      num_words = util.shape(mention_doc, 0) # T
      num_c = util.shape(span_starts, 0) # NC
      antecedents_mask, antecedents, top_antecedent_offsets = self.get_antecedent_mask(num_c)
      # encoded_doc_end = encoded_doc_start = encoded_doc
      ac_scores = self.gather_twice(self.get_bilinear_scores_xWy(mention_doc, 'W_ac', antecedent_doc), span_starts, tf.tile(tf.expand_dims(span_starts, 0), [num_c, 1]))
      ad_scores = self.gather_twice(self.get_bilinear_scores_xWy(mention_doc, 'W_ad', antecedent_doc), span_starts,  tf.tile(tf.expand_dims(span_ends, 0), [num_c, 1]))
      bc_scores = self.gather_twice(self.get_bilinear_scores_xWy(mention_doc, 'W_bc', antecedent_doc), span_ends,  tf.tile(tf.expand_dims(span_starts, 0), [num_c, 1]))
      bd_scores = self.gather_twice(self.get_bilinear_scores_xWy(mention_doc, 'W_bd', antecedent_doc), span_ends,  tf.tile(tf.expand_dims(span_ends, 0), [num_c, 1]))
      if self.config['use_features']:
        candidate_mention_scores += self.get_width_scores(mention_doc, span_starts, span_ends)
      top_antecedent_scores =  tf.expand_dims(candidate_mention_scores, 1) + tf.expand_dims(candidate_mention_scores, 0) # [k, k]
      top_antecedent_scores  += tf.log(tf.to_float(antecedents_mask)) # [k, k]
      top_antecedent_scores +=  ac_scores + bd_scores + bc_scores + ad_scores#[nc, nc]
      if self.config['mean_word_pairs']:
        pair_scores = self.get_bilinear_scores_xWy(encoded_doc_start, 'W_pair', encoded_doc_end) # [T, T]
        masked_mention_word_scores = self.get_masked_mention_word_scores(encoded_doc, span_starts, span_ends) # [K, T]
        range_scores = tf.matmul(masked_mention_word_scores, pair_scores) # [K, T]
        pair_antecedent_scores = tf.matmul(range_scores, masked_mention_word_scores, transpose_b=True) #[K, K]

        top_antecedent_scores += pair_antecedent_scores
      if self.config['use_features']:
        antecedent_distance_buckets = self.bucket_distance(top_antecedent_offsets) # [k, c]
        # antecedent_distance_emb = tf.gather(tf.get_variable("antecedent_distance_emb", [10, self.config["feature_size"]]), antecedent_distance_buckets) # [k, c]
        # span_start_embed = util.projection(tf.gather(encoded_doc, span_starts), self.config['feature_size'], initializer=tf.truncated_normal_initializer(stddev=0.02)) 
        # antecedent_distance_scores = tf.matmul(tf.expand_dims(span_start_embed, 1), tf.transpose(antecedent_distance_emb, [0, 2, 1]) )
        # top_antecedent_scores += tf.squeeze(antecedent_distance_scores, 1)

        distance_scores = util.projection(tf.nn.dropout(tf.get_variable("antecedent_distance_emb", [10, self.config["feature_size"]]), self.dropout), 1, initializer=tf.truncated_normal_initializer(stddev=0.02)) #[10, 1]
        antecedent_distance_scores = tf.gather(tf.squeeze(distance_scores, 1), antecedent_distance_buckets) # [k, c]
        top_antecedent_scores += antecedent_distance_scores


      # top_antecedent_scores =  tf.tile(tf.expand_dims(candidate_mention_scores, 1), [1, num_c]) + tf.tile(tf.expand_dims(candidate_mention_scores, 0), [num_c, 1])  
      top_antecedent_scores  += tf.log(tf.to_float(antecedents_mask)) # [k, k]
      c = num_c #tf.minimum(500, num_c)
      _, top_antecedents = tf.nn.top_k(top_antecedent_scores, c, sorted=False) # [nc, c]

      k = util.shape(top_antecedent_scores, 0)
      top_antecedents_mask = util.batch_gather(antecedents_mask, top_antecedents) # [nc, c]
      top_antecedent_scores = util.batch_gather(top_antecedent_scores, top_antecedents) # [k, c]
      top_antecedent_offsets = util.batch_gather(top_antecedent_offsets, top_antecedents) # [k, c]

      # print( top_antecedents.get_shape() , top_antecedent_scores.get_shape()) 
      # return top_antecedent_scores, antecedents, antecedents_mask
      return top_antecedent_scores, top_antecedents, top_antecedents_mask

  def get_antecedent_mask(self, k):
    top_span_range = tf.range(k) # [k]
    antecedent_offsets = tf.expand_dims(top_span_range, 1) - tf.expand_dims(top_span_range, 0) # [k, k]
    antecedents_mask = antecedent_offsets >= 1 # [k, k]
    return antecedents_mask, tf.tile(tf.expand_dims(top_span_range, 0), [k, 1]), antecedent_offsets

  def gather_twice(self, params, indices1, indices2):
      # print('twice', params.get_shape(), indices1.get_shape(), indices2.get_shape())
      gather1_op = tf.gather(params, indices1, axis=0)
      # gather1_op = tf.Print(gather1_op, [tf.shape(gather1_op)], 'gather1')
      output = util.batch_gather(gather1_op, indices2)
      # output = tf.Print(output, [tf.shape(output)], 'gather2')
      return output

  def get_bilinear_scores_xWy(self, x, scope_W, y):
      # with tf.variable_scope(scope_W + '_start'):
        # x = util.projection(x, self.config['ffnn_size'], initializer=tf.truncated_normal_initializer(stddev=0.02))
      # with tf.variable_scope(scope_W + '_end'):
        # y = util.projection(y, self.config['ffnn_size'], initializer=tf.truncated_normal_initializer(stddev=0.02))
      with tf.variable_scope(scope_W):
        x = tf.nn.dropout(x, self.dropout)
        xW = tf.nn.dropout(util.projection(x, util.shape(x, -1), initializer=tf.truncated_normal_initializer(stddev=0.02)), self.dropout) # [k, emb]
        y = tf.nn.dropout(y, self.dropout) # [k, emb]
        output = tf.matmul(xW, y, transpose_b=True) # [k, k]
        # print(output.get_shape())
        return output


  def softmax_loss(self, antecedent_scores, antecedent_labels):
    gold_scores = antecedent_scores + tf.log(tf.to_float(antecedent_labels)) # [k, max_ant + 1]
    marginalized_gold_scores = tf.reduce_logsumexp(gold_scores, [1]) # [k]
    log_norm = tf.reduce_logsumexp(antecedent_scores, [1]) # [k]
    return log_norm - marginalized_gold_scores # [k]

  def bucket_distance(self, distances):
    """
    Places the given values (designed for distances) into 10 semi-logscale buckets:
    [0, 1, 2, 3, 4, 5-7, 8-15, 16-31, 32-63, 64+].
    """
    logspace_idx = tf.to_int32(tf.floor(tf.log(tf.to_float(distances))/math.log(2))) + 3
    use_identity = tf.to_int32(distances <= 4)
    combined_idx = use_identity * distances + (1 - use_identity) * logspace_idx
    return tf.clip_by_value(combined_idx, 0, 9)

  def get_slow_antecedent_scores(self, top_span_emb, top_antecedents, top_antecedent_emb, top_antecedent_offsets, top_span_speaker_ids, genre_emb):
    k = util.shape(top_span_emb, 0)
    c = util.shape(top_antecedents, 1)

    feature_emb_list = []

    if self.config["use_metadata"]:
      top_antecedent_speaker_ids = tf.gather(top_span_speaker_ids, top_antecedents) # [k, c]
      same_speaker = tf.equal(tf.expand_dims(top_span_speaker_ids, 1), top_antecedent_speaker_ids) # [k, c]
      speaker_pair_emb = tf.gather(tf.get_variable("same_speaker_emb", [2, self.config["feature_size"]]), tf.to_int32(same_speaker)) # [k, c, emb]
      # speaker_pair_emb = tf.Print(speaker_pair_emb, [tf.shape(speaker_pair_emb)], 'spkslow')
      feature_emb_list.append(speaker_pair_emb)

      tiled_genre_emb = tf.tile(tf.expand_dims(tf.expand_dims(genre_emb, 0), 0), [k, c, 1]) # [k, c, emb]
      feature_emb_list.append(tiled_genre_emb)

    if self.config["use_features"]:
      antecedent_distance_buckets = self.bucket_distance(top_antecedent_offsets) # [k, c]
      antecedent_distance_emb = tf.gather(tf.get_variable("antecedent_distance_emb", [10, self.config["feature_size"]]), antecedent_distance_buckets) # [k, c]
      #antecedent_dist_emb = tf.Print(antecedent_distance_emb, [tf.shape(antecedent_distance_emb)], 'ant dist')
      feature_emb_list.append(antecedent_distance_emb)

    feature_emb = tf.concat(feature_emb_list, 2) # [k, c, emb]
    feature_emb = tf.nn.dropout(feature_emb, self.dropout) # [k, c, emb]

    target_emb = tf.expand_dims(top_span_emb, 1) # [k, 1, emb]
    similarity_emb = top_antecedent_emb * target_emb # [k, c, emb]
    target_emb = tf.tile(target_emb, [1, c, 1]) # [k, c, emb]

    pair_emb = tf.concat([target_emb, top_antecedent_emb, similarity_emb, feature_emb], 2) # [k, c, emb]

    with tf.variable_scope("slow_antecedent_scores"):
      slow_antecedent_scores = util.ffnn(pair_emb, self.config["ffnn_depth"], self.config["ffnn_size"], 1, self.dropout) # [k, c, 1]
    slow_antecedent_scores = tf.squeeze(slow_antecedent_scores, 2) # [k, c]
    return slow_antecedent_scores # [k, c]

  def get_fast_antecedent_scores(self, top_span_emb):
    with tf.variable_scope("src_projection"):
      source_top_span_emb = tf.nn.dropout(util.projection(top_span_emb, util.shape(top_span_emb, -1)), self.dropout) # [k, emb]
    target_top_span_emb = tf.nn.dropout(top_span_emb, self.dropout) # [k, emb]
    return tf.matmul(source_top_span_emb, target_top_span_emb, transpose_b=True) # [k, k]

  def flatten_emb_by_sentence(self, emb, text_len_mask):
    num_sentences = tf.shape(emb)[0]
    max_sentence_length = tf.shape(emb)[1]

    emb_rank = len(emb.get_shape())
    if emb_rank  == 2:
      flattened_emb = tf.reshape(emb, [num_sentences * max_sentence_length])
    elif emb_rank == 3:
      flattened_emb = tf.reshape(emb, [num_sentences * max_sentence_length, util.shape(emb, 2)])
    else:
      raise ValueError("Unsupported rank: {}".format(emb_rank))
    return tf.boolean_mask(flattened_emb, tf.reshape(text_len_mask, [num_sentences * max_sentence_length]))


  def get_predicted_antecedents(self, antecedents, antecedent_scores):
    predicted_antecedents = []
    for i, index in enumerate(np.argmax(antecedent_scores, axis=1) - 1):
      if index < 0:
        predicted_antecedents.append(-1)
      else:
        predicted_antecedents.append(antecedents[i, index])
    return predicted_antecedents

  def get_predicted_clusters(self, top_span_starts, top_span_ends, predicted_antecedents):
    mention_to_predicted = {}
    predicted_clusters = []
    for i, predicted_index in enumerate(predicted_antecedents):
      if predicted_index < 0:
        continue
      assert i > predicted_index, (i, predicted_index)
      predicted_antecedent = (int(top_span_starts[predicted_index]), int(top_span_ends[predicted_index]))
      if predicted_antecedent in mention_to_predicted:
        predicted_cluster = mention_to_predicted[predicted_antecedent]
      else:
        predicted_cluster = len(predicted_clusters)
        predicted_clusters.append([predicted_antecedent])
        mention_to_predicted[predicted_antecedent] = predicted_cluster

      mention = (int(top_span_starts[i]), int(top_span_ends[i]))
      predicted_clusters[predicted_cluster].append(mention)
      mention_to_predicted[mention] = predicted_cluster

    predicted_clusters = [tuple(pc) for pc in predicted_clusters]
    mention_to_predicted = { m:predicted_clusters[i] for m,i in mention_to_predicted.items() }

    return predicted_clusters, mention_to_predicted

  def evaluate_coref(self, top_span_starts, top_span_ends, predicted_antecedents, gold_clusters, evaluator):
    gold_clusters = [tuple(tuple(m) for m in gc) for gc in gold_clusters]
    mention_to_gold = {}
    for gc in gold_clusters:
      for mention in gc:
        mention_to_gold[mention] = gc

    predicted_clusters, mention_to_predicted = self.get_predicted_clusters(top_span_starts, top_span_ends, predicted_antecedents)
    evaluator.update(predicted_clusters, gold_clusters, mention_to_predicted, mention_to_gold)
    return predicted_clusters

  def load_eval_data(self):
    if self.eval_data is None:
      def load_line(line):
        example = json.loads(line)
        return self.tensorize_example(example, is_training=False), example
      with open(self.config["eval_path"]) as f:
        self.eval_data = [load_line(l) for l in f.readlines()]
      num_words = sum(tensorized_example[2].sum() for tensorized_example, _ in self.eval_data)
      print("Loaded {} eval examples.".format(len(self.eval_data)))

  def evaluate(self, session, global_step=None, official_stdout=False):
    self.load_eval_data()

    coref_predictions = {}
    coref_evaluator = metrics.CorefEvaluator()
    losses = []
    doc_keys = []

    for example_num, (tensorized_example, example) in enumerate(self.eval_data):
      _, _, _, _, _, _, gold_starts, gold_ends, _, _ = tensorized_example
      feed_dict = {i:t for i,t in zip(self.input_tensors, tensorized_example)}
      # if tensorized_example[0].shape[0] <= 1:
        # print('Skipping...', tensorized_example[0].shape)
        # continue
      doc_keys.append(example['doc_key'])
      loss, (candidate_starts, candidate_ends, candidate_mention_scores, top_span_starts, top_span_ends, top_antecedents, top_antecedent_scores) = session.run([self.loss, self.predictions], feed_dict=feed_dict)
      # losses.append(session.run(self.loss, feed_dict=feed_dict))
      losses.append(loss)
      predicted_antecedents = self.get_predicted_antecedents(top_antecedents, top_antecedent_scores)
      coref_predictions[example["doc_key"]] = self.evaluate_coref(top_span_starts, top_span_ends, predicted_antecedents, example["clusters"], coref_evaluator)
      if example_num % 10 == 0:
        print("Evaluated {}/{} examples.".format(example_num + 1, len(self.eval_data)))

    summary_dict = {}
    with open('doc_keys', 'w') as f:
      for key in doc_keys:
        f.write(key + '\n')
    print(global_step, len(losses), sum(losses) / len(losses))
    # conll_results = conll.evaluate_conll(self.config["conll_eval_path"], coref_predictions, official_stdout)
    # average_f1 = sum(results["f"] for results in conll_results.values()) / len(conll_results)
    # summary_dict["Average F1 (conll)"] = average_f1
    # print("Average F1 (conll): {:.2f}%".format(average_f1))

    p,r,f = coref_evaluator.get_prf()
    summary_dict["Average F1 (py)"] = f
    print("Average F1 (py): {:.2f}%".format(f * 100))
    summary_dict["Average precision (py)"] = p
    print("Average precision (py): {:.2f}%".format(p * 100))
    summary_dict["Average recall (py)"] = r
    print("Average recall (py): {:.2f}%".format(r * 100))

    return util.make_summary(summary_dict), f