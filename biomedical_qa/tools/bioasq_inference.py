import json
import os

import tensorflow as tf

from biomedical_qa.data.bioasq_squad_builder import BioAsqSquadBuilder
from biomedical_qa.data.entity_tagger import get_entity_tagger
from biomedical_qa.inference.inference import Inferrer, get_session, get_model
from biomedical_qa.sampling.squad import SQuADSampler

tf.app.flags.DEFINE_string('bioasq_file', None, 'Path to the BioASQ JSON file.')
tf.app.flags.DEFINE_string('out_file', None, 'Path to the output file.')
tf.app.flags.DEFINE_string('model_config', None, 'Path to the Model config.')
tf.app.flags.DEFINE_string('model_weights', None, 'Path to the Model weights.')
tf.app.flags.DEFINE_string("devices", "/cpu:0", "Use this device.")

tf.app.flags.DEFINE_integer("batch_size", 32, "Number of examples in each batch.")

tf.app.flags.DEFINE_integer("beam_size", 5, "Beam size used for decoding.")

tf.app.flags.DEFINE_float("list_answer_prob_threshold", 0.04, "Beam size used for decoding.")

FLAGS = tf.app.flags.FLAGS


def load_dataset(path):

    with open(path) as f:
        bioasq_json = json.load(f)

    squad_json = BioAsqSquadBuilder(bioasq_json) \
                    .build() \
                    .get_result_object()

    return bioasq_json, squad_json


def insert_answers(bioasq_json, answers):
    """Inserts answers into bioasq_json from a
    <question id> -> InferenceResult map."""

    questions = []

    for question in bioasq_json["questions"]:
        q_id = question["id"]

        if q_id in answers:

            if question["type"] == "list":
                answer_strings = [answer_string
                                  for answer_string, answer_prob in answers[q_id]
                                  if answer_prob > FLAGS.list_answer_prob_threshold]
            else:
                answer_strings = answers[q_id].answer_strings[:5]

            if len(answer_strings) == 0:
                answer_strings = [answers[q_id].answer_strings[0]]

            question["exact_answer"] = [[s] for s in answer_strings]
            question["ideal_answer"] = ""
            questions.append(question)

    return {"questions": questions}


if __name__ == "__main__":

    devices = FLAGS.devices.split(",")

    sess = get_session()
    model = get_model(sess, FLAGS.model_config, devices, FLAGS.model_weights)
    inferrer = Inferrer(model, sess, FLAGS.beam_size)

    # Build sampler from dataset JSON
    bioasq_json, squad_json = load_dataset(FLAGS.bioasq_file)
    tagger = get_entity_tagger()
    sampler = SQuADSampler(None, None, FLAGS.batch_size,
                           inferrer.models[0].embedder.vocab,
                           shuffle=False, dataset_json=squad_json,
                           tagger=tagger)

    contexts = {p["qas"][0]["id"] : p["context_original_capitalization"]
                for p in squad_json["data"][0]["paragraphs"]}
    answers = inferrer.get_predictions(sampler)
    bioasq_json = insert_answers(bioasq_json, answers)

    os.makedirs(os.path.dirname(FLAGS.out_file), exist_ok=True)
    with open(FLAGS.out_file, "w") as f:
        json.dump(bioasq_json, f, indent=2, sort_keys=True)
