"""
Decodes sequences of tags, e.g., POS tags, given a list of contextualized word embeddings
"""

from typing import Optional, Any, Dict, List, Union
from overrides import overrides
import logging

import numpy
import torch
from torch.nn.modules.linear import Linear
from torch.nn.modules.adaptive import AdaptiveLogSoftmaxWithLoss
import torch.nn.functional as F

from allennlp.data import Vocabulary
from allennlp.modules import TimeDistributed, Seq2SeqEncoder, Embedding
from allennlp.models.model import Model
from allennlp.nn import InitializerApplicator, RegularizerApplicator
# from allennlp.nn.util import sequence_cross_entropy_with_logits
from allennlp.training.metrics import CategoricalAccuracy, SpanBasedF1Measure
from udify.dataset_readers.universal_dependencies import get_all_relative_encodings
from udify.dataset_readers.lemma_edit import apply_lemma_rule

logger = logging.getLogger(__name__)


def sequence_cross_entropy_with_logits(logits: torch.FloatTensor,
                                       targets: torch.LongTensor,
                                       weights: torch.FloatTensor,
                                       average: str = "batch",
                                       label_smoothing: float = None,
                                       gamma: float = None,
                                       alpha: Union[float, List[float], torch.FloatTensor] = None
                                      ) -> torch.FloatTensor:
    """
    Computes the cross entropy loss of a sequence, weighted with respect to
    some user provided weights. Note that the weighting here is not the same as
    in the :func:`torch.nn.CrossEntropyLoss()` criterion, which is weighting
    classes; here we are weighting the loss contribution from particular elements
    in the sequence. This allows loss computations for models which use padding.

    Parameters
    ----------
    logits : ``torch.FloatTensor``, required.
        A ``torch.FloatTensor`` of size (batch_size, sequence_length, num_classes)
        which contains the unnormalized probability for each class.
    targets : ``torch.LongTensor``, required.
        A ``torch.LongTensor`` of size (batch, sequence_length) which contains the
        index of the true class for each corresponding step.
    weights : ``torch.FloatTensor``, required.
        A ``torch.FloatTensor`` of size (batch, sequence_length)
    average: str, optional (default = "batch")
        If "batch", average the loss across the batches. If "token", average
        the loss across each item in the input. If ``None``, return a vector
        of losses per batch element.
    label_smoothing : ``float``, optional (default = None)
        Whether or not to apply label smoothing to the cross-entropy loss.
        For example, with a label smoothing value of 0.2, a 4 class classification
        target would look like ``[0.05, 0.05, 0.85, 0.05]`` if the 3rd class was
        the correct label.
    gamma : ``float``, optional (default = None)
        Focal loss[*] focusing parameter ``gamma`` to reduces the relative loss for
        well-classified examples and put more focus on hard. The greater value
        ``gamma`` is, the more focus on hard examples.
    alpha : ``float`` or ``List[float]``, optional (default = None)
        Focal loss[*] weighting factor ``alpha`` to balance between classes. Can be
        used independently with ``gamma``. If a single ``float`` is provided, it
        is assumed binary case using ``alpha`` and ``1 - alpha`` for positive and
        negative respectively. If a list of ``float`` is provided, with the same
        length as the number of classes, the weights will match the classes.
        [*] T. Lin, P. Goyal, R. Girshick, K. He and P. Dollár, "Focal Loss for
        Dense Object Detection," 2017 IEEE International Conference on Computer
        Vision (ICCV), Venice, 2017, pp. 2999-3007.

    Returns
    -------
    A torch.FloatTensor representing the cross entropy loss.
    If ``average=="batch"`` or ``average=="token"``, the returned loss is a scalar.
    If ``average is None``, the returned loss is a vector of shape (batch_size,).

    """
    if average not in {None, "token", "batch"}:
        raise ValueError("Got average f{average}, expected one of "
                         "None, 'token', or 'batch'")

    # make sure weights are float
    weights = weights.float()

    # Make weights be of the right shape (i.e., extend a dimension to NUM_CLASSES)
    #NUM_CLASSES = logits.size(-1)
    #weights = weights.unsqueeze_(-1)
    #weights = weights.expand(weights.shape[0], weights.shape[1], NUM_CLASSES)

    # sum all dim except batch
    non_batch_dims = tuple(range(1, len(weights.shape)))
    # shape : (batch_size,)
    weights_batch_sum = weights.sum(dim=non_batch_dims)
    # shape : (batch * sequence_length, num_classes)
    logits_flat = logits.view(-1, logits.size(-1))

    # Use log_sigmoid instead of log_softmax
    # log_probs_flat = torch.nn.functional.logsigmoid(logits_flat)
    # shape : (batch * sequence_length, num_classes)
    # which dimension should be used to calculate the log softmax, i.e. in which dimension the class logits are located
    log_probs_flat = torch.nn.functional.log_softmax(logits_flat, dim=-1)

    label_smoothing = None

    # Make the target handle NUM_CLASSES instead of one-best
    # shape : (batch * max_len, NUM_CLASSES)
    # targets_flat = targets.view(-1, NUM_CLASSES)
    # shape : (batch * max_len, 1)
    targets_flat = targets.view(-1, 1).long()

    # focal loss coefficient
    if gamma:
        # shape : (batch * sequence_length, num_classes)
        probs_flat = log_probs_flat.exp()
        # shape : (batch * sequence_length,)
        probs_flat = torch.gather(probs_flat, dim=1, index=targets_flat)
        # shape : (batch * sequence_length,)
        focal_factor = (1. - probs_flat) ** gamma
        # shape : (batch, sequence_length)
        focal_factor = focal_factor.view(*targets.size())
        weights = weights * focal_factor

    if alpha is not None:
        # shape : () / (num_classes,)
        if isinstance(alpha, (float, int)):
            # pylint: disable=not-callable
            # shape : (2,)
            alpha_factor = torch.tensor([1. - float(alpha), float(alpha)],
                                        dtype=weights.dtype, device=weights.device)
            # pylint: enable=not-callable
        elif isinstance(alpha, (list, numpy.ndarray, torch.Tensor)):
            # pylint: disable=not-callable
            # shape : (c,)
            alpha_factor = torch.tensor(alpha, dtype=weights.dtype, device=weights.device)
            # pylint: enable=not-callable
            if not alpha_factor.size():
                # shape : (1,)
                alpha_factor = alpha_factor.view(1)
                # shape : (2,)
                alpha_factor = torch.cat([1 - alpha_factor, alpha_factor])
        else:
            raise TypeError(('alpha must be float, list of float, or torch.FloatTensor, '
                             '{} provided.').format(type(alpha)))
        # shape : (batch, max_len)
        alpha_factor = torch.gather(alpha_factor, dim=0, index=targets_flat.view(-1)).view(*targets.size())
        weights = weights * alpha_factor

    if label_smoothing is not None and label_smoothing > 0.0:
        num_classes = logits.size(-1)
        smoothing_value = label_smoothing / num_classes
        # Fill all the correct indices with 1 - smoothing value.

        one_hot_targets = torch.zeros_like(log_probs_flat).scatter_(-1, targets_flat, 1.0 - label_smoothing)
        smoothed_targets = one_hot_targets + smoothing_value
        negative_log_likelihood_flat = - log_probs_flat * smoothed_targets

        # Keep all the classes instead of only the best one
        negative_log_likelihood_flat = negative_log_likelihood_flat.sum(-1, keepdim=True)
    else:
        # Contribution to the negative log likelihood only comes from the exact indices
        # of the targets, as the target distributions are one-hot. Here we use torch.gather
        # to extract the indices of the num_classes dimension which contribute to the loss.
        # shape : (batch * sequence_length, 1)
        negative_log_likelihood_flat = - torch.gather(log_probs_flat, dim=1, index=targets_flat)
    # shape : (batch, sequence_length)
    negative_log_likelihood = negative_log_likelihood_flat.view(*targets.size())
    # shape : (batch, sequence_length)
    negative_log_likelihood = negative_log_likelihood * weights

    #negative_log_likelihood = torch.nn.functional.binary_cross_entropy_with_logits(logits_flat, targets_flat.type_as(logits_flat), reduction='none')
    #negative_log_likelihood = negative_log_likelihood.view(*targets.size())

    if average == "batch":
        # shape : (batch_size,)
        per_batch_loss = negative_log_likelihood.sum(non_batch_dims) / (weights_batch_sum + 1e-13)
        num_non_empty_sequences = ((weights_batch_sum > 0).float().sum() + 1e-13)
        return per_batch_loss.sum() / num_non_empty_sequences
    elif average == "token":
        return negative_log_likelihood.sum() / (weights_batch_sum.sum() + 1e-13)
    else:
        # shape : (batch_size,)
        per_batch_loss = negative_log_likelihood.sum(non_batch_dims) / (weights_batch_sum + 1e-13)
        return per_batch_loss


def sequence_cross_entropy(log_probs: torch.FloatTensor,
                           targets: torch.LongTensor,
                           weights: torch.FloatTensor,
                           average: str = "batch",
                           label_smoothing: float = None) -> torch.FloatTensor:
    if average not in {None, "token", "batch"}:
        raise ValueError("Got average f{average}, expected one of "
                         "None, 'token', or 'batch'")
    # shape : (batch * sequence_length, num_classes)
    log_probs_flat = log_probs.view(-1, log_probs.size(2))
    # shape : (batch * max_len, 1)
    targets_flat = targets.view(-1, 1).long()

    if label_smoothing is not None and label_smoothing > 0.0:
        num_classes = log_probs.size(-1)
        smoothing_value = label_smoothing / num_classes
        # Fill all the correct indices with 1 - smoothing value.
        one_hot_targets = torch.zeros_like(log_probs_flat).scatter_(-1, targets_flat, 1.0 - label_smoothing)
        smoothed_targets = one_hot_targets + smoothing_value
        negative_log_likelihood_flat = - log_probs_flat * smoothed_targets
        negative_log_likelihood_flat = negative_log_likelihood_flat.sum(-1, keepdim=True)
    else:
        # Contribution to the negative log likelihood only comes from the exact indices
        # of the targets, as the target distributions are one-hot. Here we use torch.gather
        # to extract the indices of the num_classes dimension which contribute to the loss.
        # shape : (batch * sequence_length, 1)
        negative_log_likelihood_flat = - torch.gather(log_probs_flat, dim=1, index=targets_flat)
    # shape : (batch, sequence_length)
    negative_log_likelihood = negative_log_likelihood_flat.view(*targets.size())
    # shape : (batch, sequence_length)
    negative_log_likelihood = negative_log_likelihood * weights.float()

    if average == "batch":
        # shape : (batch_size,)
        per_batch_loss = negative_log_likelihood.sum(1) / (weights.sum(1).float() + 1e-13)
        num_non_empty_sequences = ((weights.sum(1) > 0).float().sum() + 1e-13)
        return per_batch_loss.sum() / num_non_empty_sequences
    elif average == "token":
        return negative_log_likelihood.sum() / (weights.sum().float() + 1e-13)
    else:
        # shape : (batch_size,)
        per_batch_loss = negative_log_likelihood.sum(1) / (weights.sum(1).float() + 1e-13)
        return per_batch_loss


@Model.register("udify_tag_decoder")
class TagDecoder(Model):
    """
    A basic sequence tagger that decodes from inputs of word embeddings
    """
    def __init__(self,
                 vocab: Vocabulary,
                 task: str,
                 encoder: Seq2SeqEncoder,
                 prev_task: str,
                 prev_task_embed_dim: int = None,
                 label_smoothing: float = 0.0,
                 dropout: float = 0.0,
                 adaptive: bool = False,
                 features: List[str] = None,
                 metric: str = "acc",
                 loss_weight: float = 1.0,
                 initializer: InitializerApplicator = InitializerApplicator(),
                 regularizer: Optional[RegularizerApplicator] = None) -> None:
        super(TagDecoder, self).__init__(vocab, regularizer)

        self.task = task
        self.dropout = torch.nn.Dropout(p=dropout)
        self.encoder = encoder
        self.output_dim = encoder.get_output_dim()
        self.label_smoothing = label_smoothing
        self.num_classes = self.vocab.get_vocab_size(task)
        self.adaptive = adaptive
        self.features = features if features else []
        self.metric = metric
        self.loss_weight = loss_weight

        # A: add all possible relative encoding to vocabulary
        if self.vocab.get_token_index('100,root') == 1:
            for head in self.vocab.get_token_to_index_vocabulary('head_tags').keys():
                all_encodings = get_all_relative_encodings(head)
                self.vocab.add_tokens_to_namespace(tokens=all_encodings, namespace='dep_encoded')
            # make sure to put end token '100,root'
            self.vocab.add_token_to_namespace(token='100,root', namespace='dep_encoded')

        self.prev_task_tag_embedding = None
        if prev_task_embed_dim is not None and prev_task_embed_dim is not 0 and prev_task is not None:
            if not prev_task == 'rependency':
                self.prev_task_tag_embedding = Embedding(self.vocab.get_vocab_size(prev_task), prev_task_embed_dim)
            else:
                self.prev_task_tag_embedding = Embedding(self.vocab.get_vocab_size('dep_encoded'), prev_task_embed_dim)

        # Choose the metric to use for the evaluation (from the defined
        # "metric" value of the task). If not specified, default to accuracy.
        if self.metric == "acc":
            self.metrics = {"acc": CategoricalAccuracy()}
        elif self.metric == "span_f1":
            self.metrics = {"span_f1": SpanBasedF1Measure(
                self.vocab, tag_namespace=self.task, label_encoding="BIO")}
        else:
            logger.warning(f"ERROR. Metric: {self.metric} unrecognized. Using accuracy instead.")
            self.metrics = {"acc": CategoricalAccuracy()}

        if self.adaptive:
            # TODO
            adaptive_cutoffs = [round(self.num_classes / 15), 3 * round(self.num_classes / 15)]
            self.task_output = AdaptiveLogSoftmaxWithLoss(self.output_dim,
                                                          self.num_classes,
                                                          cutoffs=adaptive_cutoffs,
                                                          div_value=4.0)
        else:
            self.task_output = TimeDistributed(Linear(self.output_dim, self.num_classes))

        self.feature_outputs = torch.nn.ModuleDict()
        self.features_metrics = {}
        for feature in self.features:
            self.feature_outputs[feature] = TimeDistributed(Linear(self.output_dim,
                                                                   vocab.get_vocab_size(feature)))
            self.features_metrics[feature] = {
                "acc": CategoricalAccuracy(),
            }

        initializer(self)

    @overrides
    def forward(self,
                encoded_text: torch.FloatTensor,
                mask: torch.LongTensor,
                gold_tags: Dict[str, torch.LongTensor],
                prev_task_classes: torch.LongTensor = None,
                metadata: List[Dict[str, Any]] = None) -> Dict[str, torch.Tensor]:

        batch_size, _, _ = encoded_text.size()

        if prev_task_classes is not None and self.prev_task_tag_embedding is not None:
            if prev_task_classes[1]:
                embedded_tags = torch.matmul(prev_task_classes[0], self.prev_task_tag_embedding.weight)
            else:
                prev_embed_size = self.prev_task_tag_embedding.get_output_dim()
                embedded_tags = self.dropout(self.prev_task_tag_embedding(prev_task_classes[0]))
                embedded_tags = embedded_tags.view(batch_size, -1, prev_embed_size)
            encoded_text = torch.cat([encoded_text, embedded_tags], -1)

        hidden = encoded_text
        hidden = self.encoder(hidden, mask)

        batch_size, sequence_length, _ = hidden.size()
        output_dim = [batch_size, sequence_length, self.num_classes]

        loss_fn = self._adaptive_loss if self.adaptive else self._loss

        output_dict = loss_fn(hidden, mask, gold_tags.get(self.task, None), output_dim)
        self._features_loss(hidden, mask, gold_tags, output_dict)

        return output_dict

    def _adaptive_loss(self, hidden, mask, gold_tags, output_dim):
        logits = hidden
        reshaped_log_probs = logits.view(-1, logits.size(2))

        class_probabilities = self.task_output.log_prob(reshaped_log_probs).view(output_dim)

        output_dict = {"logits": logits, "class_probabilities": class_probabilities}

        if gold_tags is not None:
            output_dict["loss"] = sequence_cross_entropy(class_probabilities,
                                                         gold_tags,
                                                         mask,
                                                         label_smoothing=self.label_smoothing)
            for metric in self.metrics.values():
                metric(class_probabilities, gold_tags, mask.float())

        return output_dict

    def _loss(self, hidden, mask, gold_tags, output_dim):
        logits = self.task_output(hidden)
        reshaped_log_probs = logits.view(-1, self.num_classes)
        # print(reshaped_log_probs, reshaped_log_probs.shape)
        class_probabilities = F.softmax(reshaped_log_probs, dim=-1).view(output_dim)
        # print(class_probabilities, class_probabilities.shape)
        # import sys
        # sys.exit()

        output_dict = {"logits": logits, "class_probabilities": class_probabilities}

        if gold_tags is not None:
            output_dict["loss"] = self.loss_weight * sequence_cross_entropy_with_logits(logits,
                                                                     gold_tags,
                                                                     mask,
                                                                     label_smoothing=self.label_smoothing)
            for metric in self.metrics.values():
                metric(logits, gold_tags, mask.float())

        return output_dict

    def _features_loss(self, hidden, mask, gold_tags, output_dict):
        if gold_tags is None:
            return

        for feature in self.features:
            logits = self.feature_outputs[feature](hidden)
            loss = sequence_cross_entropy_with_logits(logits,
                                                      gold_tags[feature],
                                                      mask,
                                                      label_smoothing=self.label_smoothing)
            loss /= len(self.features)
            output_dict["loss"] += loss

            for metric in self.features_metrics[feature].values():
                metric(logits, gold_tags[feature], mask.float())

    @overrides
    def decode(self, output_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        all_words = output_dict["words"]

        all_predictions = output_dict["class_probabilities"][self.task].cpu().data.numpy()
        if all_predictions.ndim == 3:
            predictions_list = [all_predictions[i] for i in range(all_predictions.shape[0])]
        else:
            predictions_list = [all_predictions]
        all_tags = []
        for predictions, words in zip(predictions_list, all_words):
            argmax_indices = numpy.argmax(predictions, axis=-1)
            tags = [self.vocab.get_token_from_index(x, namespace=self.task)
                    for x in argmax_indices]

            # TODO: specific task
            if self.task == "lemmas":
                def decode_lemma(word, rule):
                    if rule == "_":
                        return "_"
                    if rule == "@@UNKNOWN@@":
                        return word
                    return apply_lemma_rule(word, rule)
                tags = [decode_lemma(word, rule) for word, rule in zip(words, tags)]

            all_tags.append(tags)
        output_dict[self.task] = all_tags

        return output_dict

    @overrides
    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        main_metrics = {
            f".run/{self.task}/{metric_name}": metric.get_metric(reset)
            for metric_name, metric in self.metrics.items()
        }

        features_metrics = {
            f"_run/{self.task}/{feature}/{metric_name}": metric.get_metric(reset)
            for feature in self.features
            for metric_name, metric in self.features_metrics[feature].items()
        }

        return {**main_metrics, **features_metrics}
