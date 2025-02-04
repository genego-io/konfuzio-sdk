"""Extract information from Documents.

Conventional template matching based approaches fail to generalize well to document images of unseen templates,
and are not robust against text recognition errors.

We follow the approach proposed by Sun et. al (2021) to encode both the visual and textual
features of detected text regions, and edges of which represent the spatial relations between neighboring text
regions. Their experiments validate that all information including visual features, textual
features and spatial relations can benefit key information extraction.

We reduce the hardware requirements from 1 NVIDIA Titan X GPUs with 12 GB memory to a 1 CPU and 16 GB memory by
replacing the end-to-end pipeline into two parts.

Sun, H., Kuang, Z., Yue, X., Lin, C., & Zhang, W. (2021). Spatial Dual-Modality Graph Reasoning for Key Information
Extraction. arXiv. https://doi.org/10.48550/ARXIV.2103.14470
"""
import bz2
import collections
import difflib
import functools
import logging
import os
import pathlib
import shutil
import sys
import time
import unicodedata
from copy import deepcopy
from heapq import nsmallest
from typing import Tuple, Optional, List, Union, Callable, Dict
from warnings import warn

import numpy
import pandas
import cloudpickle
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, precision_recall_fscore_support, f1_score, balanced_accuracy_score
from sklearn.utils.validation import check_is_fitted
from tabulate import tabulate

from konfuzio_sdk.data import Document, Annotation, Category, AnnotationSet, Label, LabelSet, Span
from konfuzio_sdk.normalize import normalize_to_float, normalize_to_date, normalize_to_percentage
from konfuzio_sdk.regex import regex_matches
from konfuzio_sdk.tokenizer.regex import WhitespaceTokenizer
from konfuzio_sdk.utils import get_timestamp, get_bbox
from konfuzio_sdk.evaluate import Evaluation

logger = logging.getLogger(__name__)

"""Multiclass classifier for document extraction."""
CANDIDATES_CACHE_SIZE = 100

warn('This module is WIP: https://gitlab.com/konfuzio/objectives/-/issues/9311', FutureWarning, stacklevel=2)


def get_offsets_per_page(doc_text: str) -> Dict:
    """Get the first start and last end offsets per page."""
    page_text = doc_text.split('\f')
    start = 0
    starts_ends_per_page = {}

    for ind, page in enumerate(page_text):
        len_page = len(page)
        end = start + len_page
        starts_ends_per_page[ind] = (start, end)
        start = end + 1

    return starts_ends_per_page


def filter_dataframe(df: pandas.DataFrame, label_name: str, labels_threshold: dict) -> pandas.DataFrame:
    """Filter dataframe rows accordingly with the Accuracy value.

    Rows (extractions) where the accuracy value is below the threshold defined for the label are removed.

    :param df: Dataframe with extraction results
    :param label_name: Name of the label
    :param labels_threshold: Dictionary with the threshold values for each label
    :returns: Filtered dataframe
    """
    try:
        _label_threshold = labels_threshold[label_name]
    except KeyError:
        _label_threshold = 0.1

    filtered = df[df['Accuracy'] >= _label_threshold]

    return filtered


def filter_low_confidence_extractions(result: Dict, labels_threshold: Dict) -> Dict:
    """Remove extractions with confidence below the threshold defined for the respective label.

    The input is a dictionary where the values can be:
    - dataframe
    - dictionary where the values are dataframes
    - list of dictionaries  where the values are dataframes

    :param result: Extraction results
    :param labels_threshold: Dictionary with the threshold values for each label
    :returns: Filtered dictionary.
    """
    for k in list(result.keys()):
        if isinstance(result[k], pandas.DataFrame):
            filtered = filter_dataframe(result[k], k, labels_threshold)
            if filtered.empty:
                del result[k]
            else:
                result[k] = filtered

        elif isinstance(result[k], list):
            for e, element in enumerate(result[k]):
                for sk in list(element.keys()):
                    if isinstance(element[sk], pandas.DataFrame):
                        filtered = filter_dataframe(result[k][e][sk], sk, labels_threshold)
                        if filtered.empty:
                            del result[k][e][sk]
                        else:
                            result[k][e][sk] = filtered

        elif isinstance(result[k], dict):
            for ssk in list(result[k].keys()):
                if isinstance(result[k][ssk], pandas.DataFrame):
                    filtered = filter_dataframe(result[k][ssk], ssk, labels_threshold)
                    if filtered.empty:
                        del result[k][ssk]
                    else:
                        result[k][ssk] = filtered

    return result


def remove_empty_dataframes_from_extraction(result: Dict) -> Dict:
    """Remove empty dataframes from the result of an Extraction AI.

    The input is a dictionary where the values can be:
    - dataframe
    - dictionary where the values are dataframes
    - list of dictionaries  where the values are dataframes
    """
    for k in list(result.keys()):
        if isinstance(result[k], pandas.DataFrame) and result[k].empty:
            del result[k]
        elif isinstance(result[k], list):
            for e, element in enumerate(result[k]):
                for sk in list(element.keys()):
                    if isinstance(element[sk], pandas.DataFrame) and element[sk].empty:
                        del result[k][e][sk]
        elif isinstance(result[k], dict):
            for ssk in list(result[k].keys()):
                if isinstance(result[k][ssk], pandas.DataFrame) and result[k][ssk].empty:
                    del result[k][ssk]

    return result


def get_bboxes_by_coordinates(doc_bbox: Dict, selection_bboxes: List[Dict]) -> List[Dict]:
    """
    Get the bboxes of the characters contained in the selection bboxes.

    todo: is this a duplicate of get_merged_bboxes in konfuzio_sdk.utils ?

    Simplifies `get_bboxes`..

    Returns a list of bboxes.
    :param doc_bbox: Bboxes of the characters in the document.
    :param selection_bboxes: Bboxes from which to get the info of the characters that they include.
    :return: List of the bboxes of the characters included in selection_bboxes.
    """
    # initialize the list of bboxes that will later be returned
    final_bboxes = []

    # convert string indexes to int
    doc_bbox = {int(index): char_bbox for index, char_bbox in doc_bbox.items()}

    # iterate through every bbox of the selection
    for selection_bbox in selection_bboxes:
        selected_bboxes = [
            # the index of the character is its offset, i.e. the number of chars before it in the document's text
            {**char_bbox}
            for index, char_bbox in doc_bbox.items()
            if selection_bbox["page_index"] == char_bbox["page_number"] - 1
            # filter the characters of the document according to their x/y values, so that we only include the
            # characters that are inside the selection
            and selection_bbox["x0"] <= char_bbox["x0"]
            and selection_bbox["x1"] >= char_bbox["x1"]
            and selection_bbox["y0"] <= char_bbox["y0"]
            and selection_bbox["y1"] >= char_bbox["y1"]
        ]

        final_bboxes.extend(selected_bboxes)

    return final_bboxes


def flush_buffer(buffer: List[pandas.Series], doc_text: str, merge_vertical=False) -> Dict:
    """
    Merge a buffer of entities into a dictionary (which will eventually be turned into a DataFrame).

    A buffer is a list of pandas.Series objects.
    """
    if 'label_text' in buffer[0]:
        label = buffer[0]['label_text']
    elif 'label' in buffer[0]:
        label = buffer[0]['label']

    # considering multiline case
    if merge_vertical:
        starts = []
        ends = []
        text = ""
        n_buf = len(buffer)
        for ind, buf in enumerate(buffer):
            starts.append(buf['Start'])
            ends.append(buf['End'])
            text += doc_text[buf['Start'] : buf['End']]
            if ind < n_buf - 1:
                text += '\n'
    else:
        starts = buffer[0]['Start']
        ends = buffer[-1]['End']
        text = doc_text[starts:ends]

    res_dict = dict()
    res_dict['Start'] = starts
    res_dict['End'] = ends
    res_dict['label'] = label
    res_dict['Candidate'] = text
    res_dict['Translated_Candidate'] = res_dict['Candidate']
    res_dict['Translation'] = None
    res_dict['Accuracy'] = numpy.mean([b['Accuracy'] for b in buffer])
    res_dict['x0'] = min([b['x0'] for b in buffer])
    res_dict['x1'] = max([b['x1'] for b in buffer])
    res_dict['y0'] = min([b['y0'] for b in buffer])
    res_dict['y1'] = max([b['y1'] for b in buffer])
    return res_dict


def is_valid_merge(
    row: pandas.Series,
    buffer: List[pandas.Series],
    doc_text: str,
    label_types: Dict[str, str],
    doc_bbox: Union[None, Dict] = None,
    offsets_per_page: Union[None, Dict] = None,
    merge_vertical: bool = False,
    threshold: float = 0.0,
    max_offset_distance: int = 5,
) -> bool:
    """
    Verify if the merging that we are trying to do is valid.

    If merging certain labels we only merge them if their merge keeps them a valid data type.

    For example if two dates are next to each other in text then we only want to merge them if the result of the merge
    is still a valid date.

    If merging vertically we only check the vertical merging condition. Everything else is skipped.

    :param row: Row candidate to be merged to what is already in the buffer.
    :param buffer: Previous information.
    :param doc_text: Text of the document.
    :param label_types: Types of the entities.
    :param doc_bbox: Bboxes of the characters in the document.
    :param offsets_per_page: Start and end offset of each page in the document.
    :param merge_vertical: Option to verify the vertical merge of the entities.
    :param threshold: Confidence threshold for the candidate to be merged.
    :param max_offset_distance: Maximum distance between two entities that can be merged.
    :return: If the merge is valid or not.
    """
    # Vertical case
    if merge_vertical:
        return is_valid_merge_vertical(row=row, buffer=buffer, doc_bbox=doc_bbox, offsets_per_page=offsets_per_page)

    # Horizontal case
    # only merge if candidate is above accuracy threshold for merging
    if threshold is None:
        threshold = 0.1

    if row['Accuracy'] < threshold:
        return False
    # only merge if all are the same data type
    if len(set(label_types)) > 1:
        return False

    if doc_bbox is not None:
        # only merge if there are no characters in between (or only maximum of 5 whitespaces)
        char_bboxes = [
            doc_bbox[str(char_bbox_id)]
            for char_bbox_id in range(buffer[-1]['End'], row['Start'])
            if str(char_bbox_id) in doc_bbox
        ]

        char_text = [chat_bbox['text'] for chat_bbox in char_bboxes]
        # Do not merge if there are characters between
        if not all([c == '' or c == ' ' for c in char_text]):
            return False

        # Do not merge if there are more than the maximum offset distance
        if len(char_text) > max_offset_distance:
            return False

    # Do not merge if the difference in the offsets is bigger than the maximum offset distance
    if row['Start'] - buffer[-1]['End'] > max_offset_distance:
        return False

    # only merge if text is on same line
    # row can include entity that is already part of the buffer (buffer: Ankerkette Meterware, row: Ankerkette)
    if '\n' in doc_text[min(buffer[0]['Start'], row['Start']) : max(buffer[-1]['End'], row['End'])]:
        return False
    # always merge if not one of these data types
    # never merge numbers or positive numbers
    if label_types[0] not in {'Number', 'Positive Number', 'Percentage', 'Date'}:
        return True
    # only merge percentages if the result of the merge is still a percentage
    if label_types[0] == 'Percentage':
        text = doc_text[buffer[0]['Start'] : row['End']]
        merge = normalize_to_percentage(text)
        return merge is not None
    # only merge date if the result of the merge is still a date
    if label_types[0] == 'Date':
        text = doc_text[buffer[0]['Start'] : row['End']]
        merge = normalize_to_date(text)
        return merge is not None
    # should only get here if we have a single data type that is either Number or Positive Number,
    # which we do not merge
    else:
        return False


def is_valid_merge_vertical(
    row: pandas.Series, buffer: List[pandas.Series], doc_bbox: Dict, offsets_per_page: Dict
) -> bool:
    """
    Verify if the vertical merging that we are trying to do is valid.

    To be valid, it has to respect 2 conditions:

    1. There is an overlap in the x coordinates of the bbox that includes the entities in the buffer and the row x
     coordinates.

    2. The bbox that includes the entities in the buffer and the row does not include any other character of the
    document.

    To check the 2nd condition, we get the bboxes of the characters from the entities in the buffer and the entity in
    the row:
    a) based on their start and end offsets
    b) based on the bounding box that includes all these entities

    b should not include any character that is not in a.

    :param row: Row candidate to be merged to what is already in the buffer.
    :param buffer: Previous information.
    :param doc_bbox: Bboxes of the characters in the document.
    :param offsets_per_page: Start and end offset of each page in the document.
    :return: If the merge is valid or not.
    """
    # Bbox formed by the entities in the buffer.
    buffer_bbox = {
        'x0': min([b['x0'] for b in buffer]),
        'x1': max([b['x1'] for b in buffer]),
        'y0': min([b['y0'] for b in buffer]),
        'y1': max([b['y1'] for b in buffer]),
    }

    # 1. There is an overlap in x coordinates
    is_overlap = (
        buffer_bbox['x1'] >= row['x0'] >= buffer_bbox['x0']
        or buffer_bbox['x1'] >= row['x1'] >= buffer_bbox['x0']
        or (buffer_bbox['x0'] >= row['x0'] and row['x1'] >= buffer_bbox['x1'])
    )  # NOQA

    if not is_overlap:
        return False

    # 2. There is no other characters if the buffer bbox is updated with the current row
    # update buffer with row
    temp_buffer = buffer.copy()
    temp_buffer.append(row)

    # get page index (necessary to select the bboxes of the characters)
    for buf in temp_buffer:
        for page_index, offsets in offsets_per_page.items():
            if buf['Start'] >= offsets[0] and buf['End'] <= offsets[1]:
                break

        buf['page_index'] = page_index

    if len(set([buf['page_index'] for buf in temp_buffer])) > 1:
        logger.info('Merging annotations across pages is not possible.')
        return False

    # get bboxes by start and end offsets of each row in the temp buffer
    bboxes_by_offset = []
    for buf in temp_buffer:
        char_bboxes = [
            doc_bbox[str(char_bbox_id)]
            for char_bbox_id in range(buf['Start'], buf['End'] + 1)
            if str(char_bbox_id) in doc_bbox
        ]
        bboxes_by_offset.extend(char_bboxes)

    # get bboxes contained in the bbox of the temp buffer
    buffer_row_bbox = {
        'x0': min([buffer_bbox['x0'], row['x0']]),
        'x1': max([buffer_bbox['x1'], row['x1']]),
        'y0': min([buffer_bbox['y0'], row['y0']]),
        'y1': max([buffer_bbox['y1'], row['y1']]),
        'page_index': temp_buffer[0]['page_index'],
    }

    bboxes_by_coordinates = get_bboxes_by_coordinates(doc_bbox, [buffer_row_bbox])

    # check if there are bboxes in the merged bbox that are not part of the ones obtained by the offsets
    diff = [x for x in bboxes_by_coordinates if x not in bboxes_by_offset and x['text'] != ' ']
    no_diffs = len(diff) == 0

    return no_diffs


def merge_df(
    df: pandas.DataFrame,
    doc_text: str,
    label_type_dict: Dict,
    doc_bbox: Union[Dict, None] = None,
    merge_vertical: bool = False,
    threshold: float = 0.0,
) -> pandas.DataFrame:
    """
    Merge a DataFrame of entities with matching predicted sections/labels.

    Merge is performed between entities which are only separated by a space.
    Stores entities to be merged in the `buffer` and then creates a dict from those entities by calling `flush_buffer`.
    All of the dicts created by `flush_buffer` are then converted into a DataFrame and then returned.
    """
    res_dicts = []
    buffer = []
    end = None

    offsets_per_page = None
    if merge_vertical:
        assert doc_bbox is not None
        if df.empty:
            return pandas.DataFrame(res_dicts)
        df.sort_values(by=['y0'])
        offsets_per_page = get_offsets_per_page(doc_text)
        label_types = []

    else:
        label_types = [label_type_dict[row['label_text']] for _, row in df.iterrows()]

    for _, row in df.iterrows():  # iterate over the rows in the DataFrame
        # skip extractions bellow threshold
        if row['Accuracy'] < threshold:
            continue
        # if they are valid merges then add to buffer
        if end and is_valid_merge(
            row, buffer, doc_text, label_types, doc_bbox, offsets_per_page, merge_vertical, threshold
        ):
            buffer.append(row)
            end = row['End']
        else:  # else, flush the buffer by creating a res_dict
            if buffer:
                res_dict = flush_buffer(buffer, doc_text, merge_vertical=merge_vertical)
                res_dicts.append(res_dict)
            buffer = []
            buffer.append(row)
            end = row['End']
    if buffer:  # flush buffer at the very end to clear anything left over
        res_dict = flush_buffer(buffer, doc_text, merge_vertical=merge_vertical)
        res_dicts.append(res_dict)
    df = pandas.DataFrame(res_dicts)  # convert the list of res_dicts created by `flush_buffer` into a DataFrame
    return df


def merge_annotations(
    res_dict: Dict,
    doc_text: str,
    label_type_dict: Dict[str, str],
    doc_bbox: Union[Dict, None] = None,
    multiline_labels_names: Union[list, None] = None,
    merge_vertical: bool = False,
    labels_threshold: Union[dict, None] = None,
) -> Dict:
    """
    Merge annotations by merging neighbouring entities in the res_dict with the same predicted section/label.

    Does so by recursively calling itself until it reaches a pandas DataFrame, at which point it performs the merging
    on the DataFrame.

    Merging is dependent on the data type of the label, e.g. we always merge 'Text', never merge 'Number', only merge
    'Percentage' and 'Date' if the resultant merge also gives a valid percentage or date.

    The merge vertical option tries to group multiline predictions of the same label into a single one and should be
    used only after the merge horizontal (the horizontal merge is skipped if the vertical is enabled).
    The merge is dependent on the overlapping of the x coordinates and the intersection with other elements in the
    document.
    For this option, the document bbox is necessary as well as the names of the labels in which the merge can occur.

    text is the text of the document.
    label_type_dict is a dictionary where the label names are keys and the values are the data type.
    doc_bbox are the bounding boxes of the characters in the document.
    multiline_labels_names is a list with the names of the labels with multiline annotations.
    merge_vertical is a bool for merging the entities vertically.

    Example:
    res_dict = {
        'Text':
            start end label  candidate
            0     5   'Text' hello
            6     10  'Text' world,
        'Number':
            start end label    candidate
            20    25  'Number' 1234
            26    30  'Number' 5678,
        'Date':
            start end label  candidate
            30    32  'Date' 01.01
            33    37  'Date' 2001
            38    48  'Date' 02.02.2002
                }
    text = document.text
    label_type_dict = {label.name: label.data_type for label in self.labels}
    merged_res_dict = merge_annotations(res_dict, text, label_type_dict)
    merged_res_dict = {
        'Text':
            start end label  candidate
            0     10  'Text' hello world,
        'Number':
            start end label    candidate
            20    25  'Number' 1234
            26    30  'Number' 5678,
        'Date':
            start end label  candidate
            30    37  'Date' 01.01 2001
            38    48  'Date' 02.02.2002
                }

    If the merge vertical is enabled, entities with the same label that respect the defined conditions are grouped.

    Example:
    res_dict = {
        'CompanyName':
            start end label         candidate
            0     4  'CompanyName'  Helm
            6     14  'CompanyName'  & Nagel,

    merged_res_dict = {
        'CompanyName':
            start   end     label          candidate
            [0, 6] [4, 14]  'CompanyName'  Helm & Nagel,

    """
    if merge_vertical:
        assert doc_bbox is not None
        assert multiline_labels_names is not None

    if labels_threshold is None:
        labels_threshold = {label_name: 0.0 for label_name, _ in label_type_dict.items()}

    merged_res_dict = dict()  # stores final results
    for section_label, items in res_dict.items():
        try:
            _fix_label_threshold = labels_threshold[section_label]
        except KeyError:
            _fix_label_threshold = 0.1

        if isinstance(items, pandas.DataFrame):  # perform merge on DataFrames within res_dict
            if merge_vertical:
                # only for the labels where multiline annotations can occur
                if section_label in multiline_labels_names:
                    merged_df = merge_df(
                        df=items,
                        doc_text=doc_text,
                        label_type_dict=label_type_dict,
                        doc_bbox=doc_bbox,
                        merge_vertical=merge_vertical,
                        threshold=_fix_label_threshold,
                    )
                else:
                    merged_df = items
            else:
                merged_df = merge_df(
                    df=items,
                    doc_text=doc_text,
                    label_type_dict=label_type_dict,
                    doc_bbox=doc_bbox,
                    threshold=_fix_label_threshold,
                )
            merged_res_dict[section_label] = merged_df
        # if the value of the res_dict is not a DataFrame then we recursively call merge_annotations on it
        elif isinstance(items, list):
            # if it's a list then it is a list of sections
            merged_res_dict[section_label] = [
                merge_annotations(
                    res_dict=i,
                    doc_text=doc_text,
                    label_type_dict=label_type_dict,
                    doc_bbox=doc_bbox,
                    multiline_labels_names=multiline_labels_names,
                    merge_vertical=merge_vertical,
                    labels_threshold=labels_threshold,
                )
                for i in items
            ]
        elif isinstance(items, dict):
            # if it's a dict then it is a res_dict within a list of sections
            merged_res_dict[section_label] = merge_annotations(
                res_dict=items,
                doc_text=doc_text,
                label_type_dict=label_type_dict,
                doc_bbox=doc_bbox,
                multiline_labels_names=multiline_labels_names,
                merge_vertical=merge_vertical,
                labels_threshold=labels_threshold,
            )
    return merged_res_dict


def split_multiline_annotations(annotations: List['Annotation'], multiline_labels: list) -> List['Annotation']:
    """
    Verify if there are annotations which involve multiple lines and split them into individual ones.

    For example, if an annotation includes 3 lines, 3 new annotations are created.
    This is necessary to have the correct offset strings.
    The offset string of an annotation is built considering only the start and end offset.
    In the multiline case, the start offset would be in the first line and the end offset in the last line.
    Everything that is in the middle would be included.

    :param annotations: Annotations to be verified.
    :return: Splitted annotations.
    """
    new_annotations = []

    for annotation in annotations:
        if annotation.is_multiline:
            # Keep track of which labels have this type of annotations. Important to limit the cases where vertical
            # merge of entities should be applied.
            if annotation.label not in multiline_labels:
                multiline_labels.append(annotation.label)

            for line_annotation in annotation.bboxes:
                bbox = {
                    'top': line_annotation['top'],
                    'bottom': line_annotation['bottom'],
                    'x0': line_annotation['x0'],
                    'x1': line_annotation['x1'],
                    'y0': line_annotation['y0'],
                    'y1': line_annotation['y1'],
                }

                # Document is necessary as input to have the offset string.
                # Other parameters are necessary because are not included in line_annotation.
                annot = Annotation(
                    document=annotation.document,
                    label=annotation.label,
                    is_correct=annotation.is_correct,
                    revised=annotation.revised,
                    annotation_set=annotation.annotation_set,
                    bbox=bbox,
                    **line_annotation,
                )
                new_annotations.append(annot)
        else:
            new_annotations.append(annotation)

    return new_annotations


def substring_count(list: list, substring: str) -> list:
    """Given a list of strings returns the occurrence of a certain substring and returns the results as a list."""
    r_list = [0] * len(list)

    for index in range(len(list)):
        r_list[index] = list[index].lower().count(substring)

    return r_list


def dict_to_dataframe(res_dict):
    """Convert a Dict to Dataframe add label as column."""
    df = pandas.DataFrame()
    for name in res_dict.keys():
        label_df = res_dict[name]
        label_df['label'] = name
        df = df.append(label_df, sort=True)
    return df


#
# # existent model classes
# MODEL_CLASSES = {'LabelSectionModel': LabelSectionModel,
#                  'DocumentModel': DocumentModel,
#                  'ParagraphModel': ParagraphModel,
#                  'CustomDocumentModel': CustomDocumentModel,
#                  'SentenceModel': SentenceModel
#                  }
#
# COMMON_PARAMETERS = ['tokenizer', 'text_vocab', 'model_type']
#
# label_section_components = ['label_vocab',
#                             'section_vocab',
#                             'label_classifier_config',
#                             'label_classifier_state_dict',
#                             'section_classifier_config',
#                             'section_classifier_state_dict',
#                             'extract_dicts']
#
# document_components = ['image_preprocessing',
#                        'image_augmentation',
#                        'category_vocab',
#                        'document_classifier_config',
#                        'document_classifier_state_dict']
#
# paragraph_components = ['tokenizer_mode',
#                         'paragraph_category_vocab',
#                         'paragraph_classifier_config',
#                         'paragraph_classifier_state_dict']
#
# sentence_components = ['sentence_tokenizer',
#                        'tokenizer_mode',
#                        'category_vocab',
#                        'classifier_config',
#                        'classifier_state_dict']
#
# label_section_components.extend(COMMON_PARAMETERS)
# document_components.extend(COMMON_PARAMETERS)
# paragraph_components.extend(COMMON_PARAMETERS)
# sentence_components.extend(COMMON_PARAMETERS)
# custom_document_model = deepcopy(document_components)
#
# # parameters that need to be saved with the model accordingly with the model type
# MODEL_PARAMETERS_TO_SAVE = {'LabelSectionModel': label_section_components,
#                             'DocumentModel': document_components,
#                             'ParagraphModel': paragraph_components,
#                             'CustomDocumentModel': custom_document_model,
#                             'SentenceModel': sentence_components,
#                             }
#
#
#
# def load_default_model(path: str):
#     """Load a model from default models."""
#     logger.info('loading model')
#
#     # load model dict
#     loaded_data = torch.load(path)
#
#     if 'model_type' not in loaded_data.keys():
#         model_type = path.split('_')[-1].split('.')[0]
#     else:
#         model_type = loaded_data['model_type']
#
#     model_class = MODEL_CLASSES[model_type]
#     model_args = MODEL_PARAMETERS_TO_SAVE[model_type]
#
#     # Verify if loaded data has all necessary components
#     assert all([arg in model_args for arg in loaded_data.keys()])
#
#     state_dict_name = [n for n in model_args if n.endswith('_state_dict')]
#
#     if len(state_dict_name) > 1:
#         # LabelSectionModel is a combination of 2 independent classifiers
#         assert model_type == 'LabelSectionModel'
#
#         label_classifier_state_dict = loaded_data['label_classifier_state_dict']
#         section_classifier_state_dict = loaded_data['section_classifier_state_dict']
#         extract_dicts = loaded_data['extract_dicts']
#
#         del loaded_data['label_classifier_state_dict']
#         del loaded_data['section_classifier_state_dict']
#         del loaded_data['extract_dicts']
#
#     else:
#         classifier_state_dict = loaded_data[state_dict_name[0]]
#         del loaded_data[state_dict_name[0]]
#
#     if 'model_type' in loaded_data.keys():
#         del loaded_data['model_type']
#
#     # create instance of the model class
#     model = model_class(projects=None, **loaded_data)
#
#     if model_type == 'LabelSectionModel':
#         # LabelSectionModel is a special case because it has 2 independent classifiers
#         # load parameters of the classifiers from saved parameters
#         model.label_classifier.load_state_dict(label_classifier_state_dict)
#         model.section_classifier.load_state_dict(section_classifier_state_dict)
#
#         # load extract dicts
#         model.extract_dicts = extract_dicts
#
#         # need to ensure classifiers start in evaluation mode
#         model.label_classifier.eval()
#         model.section_classifier.eval()
#
#     else:
#         # load parameters of the classifiers from saved parameters
#         model.classifier.load_state_dict(classifier_state_dict)
#
#         # need to ensure classifiers start in evaluation mode
#         model.classifier.eval()
#
#     return model

#
# def load_pickle(pickle_name: str, folder_path: str):
#     """
#     Load a pkl file or a pt (pytorch) file.
#
#   First check if the .pkl file exists at ./konfuzio.MODEL_ROOT/pickle_name, if not then assumes it is at ./pickle_name
#     Then, it assumes the .pkl file is compressed with bz2 and tries to extract and load it. If the pickle file is not
#     compressed with bz2 then it will throw an OSError and we then try and load the .pkl file will dill. This will then
#     throw an UnpicklingError if the file is not a pickle file, as expected.
#
#     :param pickle_name:
#     :return:
#     """
#     # https://stackoverflow.com/a/43006034/5344492
#     dill._dill._reverse_typemap['ClassType'] = type
#     pickle_path = os.path.join(folder_path, pickle_name)
#     if not os.path.isfile(pickle_path):
#         pickle_path = pickle_name

#     device = 'cpu'
#     if torch.cuda.is_available():
#         device = 'cuda'
#
#     if pickle_name.endswith('.pt'):
#         with open(pickle_path, 'rb') as f:
#             file_data = torch.load(pickle_path, map_location=torch.device(device))
#
#         if isinstance(file_data, dict):
#             # verification of str in path can be removed after all models being updated with the model_type
#             possible_names = [
#                 '_LabelSectionModel',
#                 '_DocumentModel',
#                 '_ParagraphModel',
#                 '_CustomDocumentModel',
#                 '_SentenceModel',
#             ]
#             if ('model_type' in file_data.keys() and file_data['model_type'] in MODEL_PARAMETERS_TO_SAVE.keys()) or
#               any([n in pickle_name for n in possible_names]):
#                 file_data = load_default_model(pickle_name)
#
#             else:
#                 raise NameError("Model type not recognized.")
#
#         else:
#             with open(pickle_path, 'rb') as f:
#                 file_data = torch.load(f, map_location=torch.device(device))
#     else:
#         try:
#             with bz2.open(pickle_path, 'rb') as f:
#                 file_data = dill.load(f)
#         except OSError:
#             with open(pickle_path, 'rb') as f:
#                 file_data = dill.load(f)
#
#     return file_data


def convert_to_feat(offset_string_list: list, ident_str: str = '') -> pandas.DataFrame:
    """Return a df containing all the features generated using the offset_string."""
    df = pandas.DataFrame()

    # strip all accents
    offset_string_list_accented = offset_string_list
    offset_string_list = [strip_accents(s) for s in offset_string_list]

    # gets the return lists for all the features
    df[ident_str + "feat_vowel_len"] = [vowel_count(s) for s in offset_string_list]
    df[ident_str + "feat_special_len"] = [special_count(s) for s in offset_string_list]
    df[ident_str + "feat_space_len"] = [space_count(s) for s in offset_string_list]
    df[ident_str + "feat_digit_len"] = [digit_count(s) for s in offset_string_list]
    df[ident_str + "feat_len"] = [len(s) for s in offset_string_list]
    df[ident_str + "feat_upper_len"] = [upper_count(s) for s in offset_string_list]
    df[ident_str + "feat_date_count"] = [date_count(s) for s in offset_string_list]
    df[ident_str + "feat_num_count"] = [num_count(s) for s in offset_string_list]
    df[ident_str + "feat_as_float"] = [normalize_to_python_float(offset_string) for offset_string in offset_string_list]
    df[ident_str + "feat_unique_char_count"] = [unique_char_count(s) for s in offset_string_list]
    df[ident_str + "feat_duplicate_count"] = [duplicate_count(s) for s in offset_string_list]
    df[ident_str + "accented_char_count"] = [
        count_string_differences(s1, s2) for s1, s2 in zip(offset_string_list, offset_string_list_accented)
    ]

    (
        df[ident_str + "feat_year_count"],
        df[ident_str + "feat_month_count"],
        df[ident_str + "feat_day_count"],
    ) = year_month_day_count(offset_string_list)

    df[ident_str + "feat_substring_count_slash"] = substring_count(offset_string_list, "/")
    df[ident_str + "feat_substring_count_percent"] = substring_count(offset_string_list, "%")
    df[ident_str + "feat_substring_count_e"] = substring_count(offset_string_list, "e")
    df[ident_str + "feat_substring_count_g"] = substring_count(offset_string_list, "g")
    df[ident_str + "feat_substring_count_a"] = substring_count(offset_string_list, "a")
    df[ident_str + "feat_substring_count_u"] = substring_count(offset_string_list, "u")
    df[ident_str + "feat_substring_count_i"] = substring_count(offset_string_list, "i")
    df[ident_str + "feat_substring_count_f"] = substring_count(offset_string_list, "f")
    df[ident_str + "feat_substring_count_s"] = substring_count(offset_string_list, "s")
    df[ident_str + "feat_substring_count_oe"] = substring_count(offset_string_list, "ö")
    df[ident_str + "feat_substring_count_ae"] = substring_count(offset_string_list, "ä")
    df[ident_str + "feat_substring_count_ue"] = substring_count(offset_string_list, "ü")
    df[ident_str + "feat_substring_count_er"] = substring_count(offset_string_list, "er")
    df[ident_str + "feat_substring_count_str"] = substring_count(offset_string_list, "str")
    df[ident_str + "feat_substring_count_k"] = substring_count(offset_string_list, "k")
    df[ident_str + "feat_substring_count_r"] = substring_count(offset_string_list, "r")
    df[ident_str + "feat_substring_count_y"] = substring_count(offset_string_list, "y")
    df[ident_str + "feat_substring_count_en"] = substring_count(offset_string_list, "en")
    df[ident_str + "feat_substring_count_g"] = substring_count(offset_string_list, "g")
    df[ident_str + "feat_substring_count_ch"] = substring_count(offset_string_list, "ch")
    df[ident_str + "feat_substring_count_sch"] = substring_count(offset_string_list, "sch")
    df[ident_str + "feat_substring_count_c"] = substring_count(offset_string_list, "c")
    df[ident_str + "feat_substring_count_ei"] = substring_count(offset_string_list, "ei")
    df[ident_str + "feat_substring_count_on"] = substring_count(offset_string_list, "on")
    df[ident_str + "feat_substring_count_ohn"] = substring_count(offset_string_list, "ohn")
    df[ident_str + "feat_substring_count_n"] = substring_count(offset_string_list, "n")
    df[ident_str + "feat_substring_count_m"] = substring_count(offset_string_list, "m")
    df[ident_str + "feat_substring_count_j"] = substring_count(offset_string_list, "j")
    df[ident_str + "feat_substring_count_h"] = substring_count(offset_string_list, "h")

    df[ident_str + "feat_substring_count_plus"] = substring_count(offset_string_list, "+")
    df[ident_str + "feat_substring_count_minus"] = substring_count(offset_string_list, "-")
    df[ident_str + "feat_substring_count_period"] = substring_count(offset_string_list, ".")
    df[ident_str + "feat_substring_count_comma"] = substring_count(offset_string_list, ",")

    df[ident_str + "feat_starts_with_plus"] = starts_with_substring(offset_string_list, "+")
    df[ident_str + "feat_starts_with_minus"] = starts_with_substring(offset_string_list, "-")

    df[ident_str + "feat_ends_with_plus"] = ends_with_substring(offset_string_list, "+")
    df[ident_str + "feat_ends_with_minus"] = ends_with_substring(offset_string_list, "-")

    return df


def starts_with_substring(list: list, substring: str) -> list:
    """Given a list of strings return 1 if string starts with the given substring for each item."""
    return [1 if s.lower().startswith(substring) else 0 for s in list]


def ends_with_substring(list: list, substring: str) -> list:
    """Given a list of strings return 1 if string starts with the given substring for each item."""
    return [1 if s.lower().endswith(substring) else 0 for s in list]


def digit_count(s: str) -> int:
    """Return the number of digits in a string."""
    return sum(c.isdigit() for c in s)


def space_count(s: str) -> int:
    """Return the number of spaces in a string."""
    return sum(c.isspace() for c in s) + s.count('\t') * 3  # Tab is already counted as one whitespace


def special_count(s: str) -> int:
    """Return the number of special (non-alphanumeric) characters in a string."""
    return sum(not c.isalnum() for c in s)


def strip_accents(s) -> str:
    """
    Strip all accents from a string.

    Source: http://stackoverflow.com/a/518232/2809427
    """
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')


def vowel_count(s: str) -> int:
    """Return the number of vowels in a string."""
    return sum(is_vowel(c) for c in s)


def count_string_differences(s1: str, s2: str) -> int:
    """Return the number of differences between two strings."""
    if len(s2) > len(s1):  # the longer string has to be s1 to catch all differences
        s1, s2 = s2, s1

    return len(''.join(x[2:] for x in difflib.ndiff(s1, s2) if x.startswith('- ')))


def is_vowel(c: str) -> bool:
    """Given a char this function returns a bool that represents if the char is a vowel or not."""
    return c.lower() in 'aeiou'


def upper_count(s: str) -> int:
    """Return the number of uppercase characters in a string."""
    return sum(c.isupper() for c in s)


def date_count(s: str) -> int:
    """
    Given a string this function tries to read it as a date (if not possible returns 0).

    If possible it returns the relative difference to 01.01.2010 in days.
    """
    # checks the format
    if len(s) > 5:
        if (s[2] == '.' and s[5] == '.') or (s[2] == '/' and s[5] == '/'):
            date1 = pandas.to_datetime("01.01.2010")
            date2 = pandas.to_datetime(s, errors='ignore')
            if date2 == s:
                return 0

            else:
                try:
                    diff = int((date2 - date1) / numpy.timedelta64(1, 'D'))
                except TypeError as e:
                    logger.error(f'Could not substract for string {s} because of >>{e}<<.')
                    return 0

            if diff == 0:
                return 1
            else:
                return diff

        else:
            return 0
    return 0


def year_month_day_count(offset_string_list: list) -> Tuple[List[int], List[int], List[int]]:
    """Given a list of offset-strings extracts the according dates, months and years for each string."""
    year_list = []
    month_list = []
    day_list = []

    assert isinstance(offset_string_list, list)

    for s in offset_string_list:
        _normalization = normalize_to_date(s)
        if _normalization:
            year_list.append(int(_normalization[:4]))
            month_list.append(int(_normalization[5:7]))
            day_list.append(int(_normalization[8:10]))
        else:
            year_list.append(0)
            month_list.append(0)
            day_list.append(0)

    return year_list, month_list, day_list


# checks if the string is a number and gives the number a value
def num_count(s: str) -> float:
    """
    Given a string this function tries to read it as a number (if not possible returns 0).

    If possible it returns the number as a float.
    """
    num = normalize_to_float(s)

    if num:
        return num
    else:
        return 0


def normalize_to_python_float(s: str) -> float:
    """
    Given a string this function tries to read it as a number using python float (if not possible returns 0).

    If possible it returns the number as a float.
    """
    try:
        f = float(s)
        if f < numpy.finfo('float32').max:
            return f
        else:
            return 0.0
    except (ValueError, TypeError):
        return 0.0


def duplicate_count(s: str) -> int:
    """Given a string this function returns the number of duplicate characters."""
    count = {}
    for c in s:
        if c in count:
            count[c] += 1
        else:
            count[c] = 1

    counter = 0
    for key in count:
        if count[key] > 1:
            counter += count[key]

    return counter


def unique_char_count(s: str) -> int:
    """Given a string returns the number of unique characters."""
    return len(set(list(s)))


def _convert_to_relative_dict(dict: dict):
    """Convert a dict with absolute numbers as values to the same dict with the relative probabilities as values."""
    return_dict = {}
    abs_num = sum(dict.values())
    for key, value in dict.items():
        return_dict[key] = value / abs_num
    return return_dict


def plot_label_distribution(df_list: list, df_name_list=None) -> None:
    """Plot the label-distribution of given DataFrames side-by-side."""
    # check if any of the input df are empty
    for df in df_list:
        if df.empty:
            logger.error('One of the Dataframes in df_list is empty.')
            return None

    # helper function
    def Convert(tup, di):
        for a, b in tup:
            di.setdefault(a, []).append(b)
        return di

    # plot the relative distributions
    logger.info('Percentage of total samples (per dataset) that have a certain label:')
    rel_dict_list = []
    for df in df_list:
        rel_dict_list.append(_convert_to_relative_dict(collections.Counter(list(df['label_text']))))
    logger.info(
        '\n'
        + tabulate(
            pandas.DataFrame(rel_dict_list, index=df_name_list).transpose(),
            floatfmt=".1%",
            headers="keys",
            tablefmt="pipe",
        )
        + '\n'
    )

    # print the number of documents in total and in the splits given
    # total_count = 0
    for index, df in enumerate(df_list):
        doc_name = df_name_list[index] if df_name_list else str(index)
        doc_count = len(set(df['document_id']))
        logger.info(doc_name + ' contains ' + str(doc_count) + ' different documents.')
        # total_count += doc_count
    # logger.info(str(total_count) + ' documents in total.')

    # plot the number of documents with at least one of a certain label
    logger.info('Percentage of documents per split that contain a certain label at least once:')
    doc_count_dict_list = []
    for df in df_list:
        doc_count_dict = {}
        doc_count = len(set(df['document_id']))
        toup_list = list(zip(list(df['label_text']), list(df['document_id'])))
        list_dict = Convert(toup_list, {})
        for key, value in list_dict.items():
            doc_count_dict[key] = float(len(set(value)) / doc_count)
        doc_count_dict_list.append(doc_count_dict)
    logger.info(
        '\n'
        + tabulate(
            pandas.DataFrame(doc_count_dict_list, index=df_name_list).transpose(),
            floatfmt=".1%",
            headers="keys",
            tablefmt="pipe",
        )
        + '\n'
    )


# def evaluate_split_quality(df_train: pandas.DataFrame, df_val: pandas.DataFrame, percentage: Optional[float] = None):
#     """Evaluate if the split method used produces satisfactory results."""
#     # check if df_train or df_val is empty
#     if df_train.empty:
#         logger.error('df_train is empty.')
#         return None
#     if df_val.empty:
#         logger.error('df_val is empty.')
#         return None
#     logger.info('Start split quality tests.')
#     n_train_examples = df_train.shape[0]
#     n_val_examples = df_val.shape[0]
#     n_total_examples = n_train_examples + n_val_examples
#
#     # check if the splits in total numbers is ok
#     if percentage and n_total_examples > 100:
#         if abs(n_train_examples / (n_total_examples * percentage) - 1) > 0.05:
#             logger.error(
#                 f'Splits differ from split percentage significantly. Percentage: {percentage}. '
#                 + f'Real Percentage: {n_train_examples / n_total_examples}'
#             )
#
#     train_dict = df_train['label_text'].value_counts().to_dict()
#     val_dict = df_val['label_text'].value_counts().to_dict()
#     total_dict = pandas.concat([df_train['label_text'], df_val['label_text']]).value_counts().to_dict()
#
#     train_dict_rel = df_train['label_text'].value_counts(normalize=True).to_dict()
#     val_dict_rel = df_val['label_text'].value_counts(normalize=True).to_dict()
#     total_dict_rel = (
#         pandas.concat([df_train['label_text'], df_val['label_text']]).value_counts(normalize=True).to_dict()
#     )
#
#     # checks the balance of the labels per split (and if there is at least one)
#     for key, value in total_dict_rel.items():
#         if key not in train_dict.keys():
#             logger.error('No sample of label "' + key + '" found in training dataset.')
#         elif total_dict[key] > 30 and abs(train_dict_rel[key] - value) > 0.05 * max(total_dict_rel[key], 0.01):
#             logger.error('Unbalanced distribution of label "' + key + '" (Significant deviation in training set)')
#         else:
#             logger.info('Balanced distribution of label "' + key + '" in training set')
#
#         if key not in val_dict.keys():
#             logger.error('No sample of label "' + key + '" found in validation dataset.')
#         elif total_dict[key] > 30 and abs(val_dict_rel[key] - value) > 0.05 * max(total_dict_rel[key], 0.01):
#             logger.warning('Unbalanced distribution of label "' + key + '" (Significant deviation in validation set)')
#         else:
#             logger.info('Balanced distribution of label "' + key + '" in validation set')
#
#     logger.info('Split quality test completed.')


# def split_in_two_by_document_df(
#     data: pandas.DataFrame, percentage: float, check_imbalances=False
# ) -> Tuple[pandas.DataFrame, pandas.DataFrame]:
#     """
#     Split the input df in two (by document) and return two dataframes of about the right size.
#
#     The first item in the return tuple is of the percentage size.
#     """
#     logger.info('Split into test and training.')
#     if data['document_id'].isnull().values.any():
#         raise Exception('To split by document_id every annotation needs a non-NaN document_id!')
#
#     df_list = [df_doc for k, df_doc in data.groupby('document_id')]
#
#     return split_in_two_by_document_df_list(data_list=df_list, percentage=percentage,
#     check_imbalances=check_imbalances)


# def split_in_two_by_document_df_list(
#     data_list: List[pandas.DataFrame], percentage: float, check_imbalances=False
# ) -> Tuple[pandas.DataFrame, pandas.DataFrame]:
#     """
#     Split a list of document df in to two concatenated df according to the percentage.
#
#     The first item in the return tuple is of the percentage size.
#     """
#     logger.info('Split into test and training.')
#     df_list = data_list
#     total_sample_num = sum([len(df.index) for df in data_list])
#     select_amount = int(total_sample_num * percentage)
#
#     selected_count = 0
#     selected_df = pandas.DataFrame()
#     rest_df = pandas.DataFrame()
#
#     random.Random(1).shuffle(df_list)
#
#     # TODO: check for maximum deviation from the percentage specified
#     for i, df_doc in enumerate(df_list):
#         # Add first document to selected_df to avoid empty df
#         if i == 0:
#             selected_df = pandas.concat([selected_df, df_doc])
#             selected_count += len(df_doc.index)
#
#         # Add second document to rest_df to avoid empty df
#         if i == 1 and percentage < 1.0:
#             rest_df = pandas.concat([rest_df, df_doc])
#             continue
#
#         # Add further documents according to required percentage.
#         if selected_count <= select_amount or percentage == 1.0:
#             selected_df = pandas.concat([selected_df, df_doc])
#             selected_count += len(df_doc.index)
#         else:
#             rest_df = pandas.concat([rest_df, df_doc])
#
#     if selected_df.empty:
#         raise Exception('Not enough data to train an AI model.')
#
#     selected_df.reset_index(drop=True, inplace=True)
#     rest_df.reset_index(drop=True, inplace=True)  # get labels used in each df
#
#     if check_imbalances:
#         selected_classes = set(selected_df['label_text'].unique())
#         rest_classes = set(rest_df['label_text'].unique())
#         # find labels that do not appear in both dfs
#         non_overlapping_classes = selected_classes ^ rest_classes
#         # remove non-overlapping examples
#         selected_df = selected_df[~selected_df['label_text'].isin(non_overlapping_classes)]
#         rest_df = rest_df[~rest_df['label_text'].isin(non_overlapping_classes)]
#         logger.info(f'The following classes could not be split and have been removed: {non_overlapping_classes}')
#     return selected_df, rest_df


def get_first_candidate(document_text, document_bbox, line_list):
    """Get the first candidate in a document."""
    # todo allow to have mult tokenizers?
    for line_num, _line in enumerate(line_list):
        line_start_offset = _line['start_offset']
        line_end_offset = _line['end_offset']
        # todo
        tokenize_fn = functools.partial(regex_matches, regex='[^ \n\t\f]+')
        for candidate in tokenize_fn(document_text[line_start_offset:line_end_offset]):
            candidate_start_offset = candidate['start_offset'] + line_start_offset
            candidate_end_offset = candidate['end_offset'] + line_start_offset
            candidate_bbox = dict(
                **get_bbox(document_bbox, candidate_start_offset, candidate_end_offset),
                offset_string=document_text[candidate_start_offset:candidate_end_offset],
                start_offset=candidate_start_offset,
                end_offset=candidate_end_offset,
            )
            return candidate_bbox


def get_line_candidates(document_text, document_bbox, line_list, line_num, candidates_cache):
    """Get the candidates from a given line_num."""
    if line_num in candidates_cache:
        return candidates_cache[line_num], candidates_cache
    line = line_list[line_num]
    line_start_offset = line['start_offset']
    line_end_offset = line['end_offset']
    line_candidates = []
    # todo see get_first_candidate
    tokenize_fn = functools.partial(regex_matches, regex='[^ \n\t\f]+')
    for candidate in tokenize_fn(document_text[line_start_offset:line_end_offset]):
        candidate_start_offset = candidate['start_offset'] + line_start_offset
        candidate_end_offset = candidate['end_offset'] + line_start_offset
        # todo: the next line is memory heavy
        #  https://gitlab.com/konfuzio/objectives/-/issues/9342
        candidate_bbox = dict(
            **get_bbox(document_bbox, candidate_start_offset, candidate_end_offset),
            offset_string=document_text[candidate_start_offset:candidate_end_offset],
            start_offset=candidate_start_offset,
            end_offset=candidate_end_offset,
        )
        line_candidates.append(candidate_bbox)
    if len(candidates_cache) >= CANDIDATES_CACHE_SIZE:
        earliest_line = min(candidates_cache.keys())
        candidates_cache.pop(earliest_line)
    candidates_cache[line_num] = line_candidates
    return line_candidates, candidates_cache


def process_document_data(
    document: Document,
    spans: List[Span],
    n_nearest: Union[int, List, Tuple] = 2,
    first_word: bool = True,
    tokenize_fn: Optional[Callable] = None,
    substring_features=None,
    catchphrase_list=None,
    n_nearest_across_lines: bool = False,
) -> Tuple[pandas.DataFrame, List, pandas.DataFrame]:
    """
    Convert the json_data from one Document to a DataFrame that can be used for training or prediction.

    Additionally returns the fake negatives, errors and conflicting annotations as a DataFrames and of course the
    column_order for training
    """
    logger.info(f'Start generating features for document {document}.')

    assert spans == sorted(spans)  # should be already sorted

    file_error_data = []
    file_data_raw = []

    if isinstance(n_nearest, int):
        n_left_nearest = n_nearest
        n_right_nearest = n_nearest
    else:
        assert isinstance(n_nearest, (tuple, list)) and len(n_nearest) == 2
        n_left_nearest, n_right_nearest = n_nearest

    l_keys = ["l_dist" + str(x) for x in range(n_left_nearest)]
    r_keys = ["r_dist" + str(x) for x in range(n_right_nearest)]

    if n_nearest_across_lines:
        l_keys += ["l_pos" + str(x) for x in range(n_left_nearest)]
        r_keys += ["r_pos" + str(x) for x in range(n_right_nearest)]

    document_bbox = document.get_bbox()
    document_text = document.text
    document_n_pages = document.number_of_pages

    if document_text is None or document_bbox == {} or len(spans) == 0:
        # if the document text is empty or if there are no ocr'd characters
        # then return an empty dataframe for the data, an empty feature list and an empty dataframe for the "error" data
        raise NotImplementedError

    line_list: List[Dict] = []
    char_counter = 0
    for line_text in document_text.replace('\f', '\n').split('\n'):
        n_chars_on_line = len(line_text)
        line_list.append({'start_offset': char_counter, 'end_offset': char_counter + n_chars_on_line})
        char_counter += n_chars_on_line + 1

    # generate the Catchphrase-Dataframe
    if catchphrase_list is not None:
        occurrence_dict = generate_catchphrase_occurrence_dict(line_list, catchphrase_list, document_text)

    if first_word:
        first_candidate = get_first_candidate(document_text, document_bbox, line_list)
        first_word_string = first_candidate['offset_string']
        first_word_x0 = first_candidate['x0']
        first_word_y0 = first_candidate['y0']
        first_word_x1 = first_candidate['x1']
        first_word_y1 = first_candidate['y1']

    # todo document.annotations () should be sorted already - check or update this function
    spans.sort(key=lambda x: x.start_offset)

    # WIP: Word on page feature
    page_text_list = document_text.split('\f')

    # used to cache the catchphrase features
    _line_num = -1
    _catchphrase_dict = None
    candidates_cache = dict()
    for span in spans:

        word_on_page_feature_list = []
        word_on_page_feature_name_list = []

        # WIP: Word on page feature
        if substring_features:
            for index, substring_feature in enumerate(substring_features):
                word_on_page_feature_list.append(substring_on_page(substring_feature, span, page_text_list))
                word_on_page_feature_name_list.append(f'word_on_page_feat{index}')
        # if span.annotation.id_:
        #     # Annotation
        #     logger.error(f'{span}')
        #     if (
        #         span.annotation.is_correct
        #         or (not span.annotation.is_correct and span.annotation.revised)
        #         or (
        #             span.annotation.confidence
        #             and hasattr(span.annotation.label, 'threshold')
        #             and span.annotation.confidence > span.annotation.label.threshold
        #         )
        #     ):
        #         pass
        #     else:
        #         logger.error(f'Annotation (ID {span.annotation.id_}) found that is not fit for the use in dataset!')

        # find the line containing the annotation
        # tokenize that line to get all candidates
        # convert each candidate into a bbox
        # append to line candidates
        # store the line_start_offset so if the next annotation is on the same line then we use the same
        # line_candidiates list and therefore saves us tokenizing the same line again
        for line_num, line in enumerate(line_list):
            if line['start_offset'] <= span.end_offset and line['end_offset'] >= span.start_offset:

                # get the catchphrase features
                if catchphrase_list is not None and len(catchphrase_list) != 0:
                    if line_num == _line_num:
                        span.catchphrase_dict = _catchphrase_dict
                    else:
                        _catchphrase_dict = generate_feature_dict_from_occurence_dict(
                            occurrence_dict, catchphrase_list, line_num
                        )
                        span.catchphrase_dict = _catchphrase_dict
                        _line_num = line_num

                line_candidates, candidates_cache = get_line_candidates(
                    document_text, document_bbox, line_list, line_num, candidates_cache
                )
                break

        l_list = []
        r_list = []

        # todo add way to calculate distance features between spans consistently
        # https://gitlab.com/konfuzio/objectives/-/issues/9688
        for candidate in line_candidates:
            try:
                span.bbox()
                if candidate['end_offset'] <= span.start_offset:
                    candidate['dist'] = span.bbox().x0 - candidate['x1']
                    candidate['pos'] = 0
                    l_list.append(candidate)
                elif candidate['start_offset'] >= span.end_offset:
                    candidate['dist'] = candidate['x0'] - span.bbox().x1
                    candidate['pos'] = 0
                    r_list.append(candidate)
            except ValueError as e:
                logger.error(f'{candidate}: {str(e)}')

        if n_nearest_across_lines:
            prev_line_candidates = []
            i = 1
            while (line_num - i) >= 0:
                line_candidates, candidates_cache = get_line_candidates(
                    document_text, document_bbox, line_list, line_num - i, tokenize_fn, candidates_cache
                )
                for candidate in line_candidates:
                    candidate['dist'] = min(
                        abs(span.x0 - candidate['x0']),
                        abs(span.x0 - candidate['x1']),
                        abs(span.x1 - candidate['x0']),
                        abs(span.x1 - candidate['x1']),
                    )
                    candidate['pos'] = -i
                prev_line_candidates.extend(line_candidates)
                if len(prev_line_candidates) >= n_left_nearest - len(l_list):
                    break
                i += 1

            next_line_candidates = []
            i = 1
            while line_num + i < len(line_list):
                line_candidates, candidates_cache = get_line_candidates(
                    document_text, document_bbox, line_list, line_num + i, tokenize_fn, candidates_cache
                )
                for candidate in line_candidates:
                    candidate['dist'] = min(
                        abs(span.x0 - candidate['x0']),
                        abs(span.x0 - candidate['x1']),
                        abs(span.x1 - candidate['x0']),
                        abs(span.x1 - candidate['x1']),
                    )
                    candidate['pos'] = i
                next_line_candidates.extend(line_candidates)
                if len(next_line_candidates) >= n_right_nearest - len(r_list):
                    break
                i += 1

        n_smallest_l_list = nsmallest(n_left_nearest, l_list, key=lambda x: x['dist'])
        n_smallest_r_list = nsmallest(n_right_nearest, r_list, key=lambda x: x['dist'])

        if n_nearest_across_lines:
            n_smallest_l_list.extend(prev_line_candidates[::-1])
            n_smallest_r_list.extend(next_line_candidates)

        while len(n_smallest_l_list) < n_left_nearest:
            n_smallest_l_list.append({'offset_string': '', 'dist': 100000, 'pos': 0})

        while len(n_smallest_r_list) < n_right_nearest:
            n_smallest_r_list.append({'offset_string': '', 'dist': 100000, 'pos': 0})

        r_list = n_smallest_r_list[:n_right_nearest]
        l_list = n_smallest_l_list[:n_left_nearest]

        # set first word features
        if first_word:
            span.first_word_x0 = first_word_x0
            span.first_word_y0 = first_word_y0
            span.first_word_x1 = first_word_x1
            span.first_word_y1 = first_word_y1
            span.first_word_string = first_word_string

        span_dict = span.eval_dict()
        # span_to_dict(span=span, include_pos=n_nearest_across_lines)

        for index, item in enumerate(l_list):
            span_dict['l_dist' + str(index)] = item['dist']
            span_dict['l_offset_string' + str(index)] = item['offset_string']
            if n_nearest_across_lines:
                span_dict['l_pos' + str(index)] = item['pos']
        for index, item in enumerate(r_list):
            span_dict['r_dist' + str(index)] = item['dist']
            span_dict['r_offset_string' + str(index)] = item['offset_string']
            if n_nearest_across_lines:
                span_dict['r_pos' + str(index)] = item['pos']

        # WIP: word on page feature
        for index, item in enumerate(word_on_page_feature_list):
            span_dict['word_on_page_feat' + str(index)] = item

        # if annotation.label and annotation.label.threshold:
        #     annotation_dict["threshold"] = annotation.label.threshold
        # else:
        #     annotation_dict["threshold"] = 0.1

        if _catchphrase_dict:
            for catchphrase, dist in _catchphrase_dict.items():
                span_dict['catchphrase_dist_' + catchphrase] = dist

        # checks for ERRORS
        if span_dict["confidence"] is None and not (span_dict["revised"] is False and span_dict["is_correct"] is True):
            file_error_data.append(span_dict)

        # adds the sample_data to the list
        if span_dict["page_index"] is not None:
            file_data_raw.append(span_dict)

    # creates the dataframe
    df = pandas.DataFrame(file_data_raw)
    df_errors = pandas.DataFrame(file_error_data)

    # first word features
    if first_word:
        df['first_word_x0'] = first_word_x0
        df['first_word_x1'] = first_word_x1
        df['first_word_y0'] = first_word_y0
        df['first_word_y1'] = first_word_y1
        df['first_word_string'] = first_word_string

        # first word string features
        df_string_features_first = convert_to_feat(list(df["first_word_string"]), "first_word_")
        string_features_first_word = list(df_string_features_first.columns.values)  # NOQA
        df = df.join(df_string_features_first, lsuffix='_caller', rsuffix='_other')
        first_word_features = ['first_word_x0', 'first_word_y0', 'first_word_x1', 'first_word_y1']

    # creates all the features from the offset string
    df_string_features_real = convert_to_feat(list(df["offset_string"]))
    string_feature_column_order = list(df_string_features_real.columns.values)

    relative_string_feature_list = []

    for index in range(n_left_nearest):
        df_string_features_l = convert_to_feat(list(df['l_offset_string' + str(index)]), 'l' + str(index) + '_')
        relative_string_feature_list += list(df_string_features_l.columns.values)
        df = df.join(df_string_features_l, lsuffix='_caller', rsuffix='_other')

    for index in range(n_right_nearest):
        df_string_features_r = convert_to_feat(list(df['r_offset_string' + str(index)]), 'r' + str(index) + '_')
        relative_string_feature_list += list(df_string_features_r.columns.values)
        df = df.join(df_string_features_r, lsuffix='_caller', rsuffix='_other')

    df["relative_position_in_page"] = df["page_index"] / document_n_pages

    abs_pos_feature_list = ["x0", "y0", "x1", "y1", "page_index", "area_quadrant_two"]  # , "area"]
    relative_pos_feature_list = ["relative_position_in_page"]

    feature_list = (
        string_feature_column_order
        + abs_pos_feature_list
        + l_keys
        + r_keys
        + relative_string_feature_list
        + relative_pos_feature_list
        + word_on_page_feature_name_list
    )
    if first_word:
        feature_list += first_word_features

    # append the catchphrase_features to the feature_list
    if catchphrase_list is not None:
        for catchphrase in catchphrase_list:
            feature_list.append('catchphrase_dist_' + catchphrase)

    # joins it to the main DataFrame
    df = df.join(df_string_features_real, lsuffix='_caller', rsuffix='_other')

    return df, feature_list, df_errors


def substring_on_page(substring, annotation, page_text_list) -> bool:
    """Check if there is an occurrence of the word on the according page."""
    if not hasattr(annotation, "page_index"):
        logger.warning("Annotation has no page_index!")
        return False
    elif annotation.page_index > len(page_text_list) - 1:
        logger.warning("Annotation's page_index does not match given text.")
        return False
    else:
        return substring in page_text_list[annotation.page_index]


def generate_catchphrase_occurrence_dict(line_list, catchphrase_list, document_text) -> Dict:
    """Generate a dict that stores on which line certain catchphrases occurrence."""
    _dict = {catchphrase: [] for catchphrase in catchphrase_list}

    for line_num, _line in enumerate(line_list):
        line_text = document_text[_line['start_offset'] : _line['end_offset']]
        for catchphrase in catchphrase_list:
            if catchphrase in line_text:
                _dict[catchphrase].append(line_num)

    return _dict


def generate_feature_dict_from_occurence_dict(occurence_dict, catchphrase_list, line_num) -> Dict:
    """Generate the fitting catchphrase features."""
    _dict = {catchphrase: None for catchphrase in catchphrase_list}

    for catchphrase in catchphrase_list:
        _dict[catchphrase] = next((i - line_num for i in occurence_dict[catchphrase] if i < line_num), -1)

    return _dict


def add_extractions_as_annotations(
    extractions: pandas.DataFrame, document: Document, label: Label, label_set: LabelSet, annotation_set: AnnotationSet
) -> None:
    """Add the extraction of a model to the document."""
    if not isinstance(extractions, pandas.DataFrame):
        raise TypeError(f'Provided extraction object should be a Dataframe, got a {type(extractions)} instead')
    if not extractions.empty:
        # TODO: define required fields
        required_fields = ['Start', 'End', 'Accuracy']
        if not set(required_fields).issubset(extractions.columns):
            raise ValueError(
                f'Extraction do not contain all required fields: {required_fields}.'
                f' Extraction columns: {extractions.columns.to_list()}'
            )

        extracted_spans = extractions[required_fields].sort_values(by='Accuracy', ascending=False)

        for span in extracted_spans.to_dict('records'):  # todo: are Start and End always ints?
            if document.bboxes is not None:
                start = span['Start']
                end = span['End']
                offset_string = document.text[start:end]
                bbox0 = document.bboxes[start]
                bbox1 = document.bboxes[end - 1]
                ann_bbox = {
                    'bottom': bbox0.page.height - bbox0.y0,
                    'end_offset': end,
                    'line_number': len(document.text[:start].split('\n')),
                    'offset_string': offset_string,
                    'offset_string_original': offset_string,
                    'page_index': bbox0.page.index,
                    'start_offset': start,
                    'top': bbox0.page.height - bbox0.y1,
                    'x0': bbox0.x0,
                    'x1': bbox1.x1,
                    'y0': bbox0.y0,
                    'y1': bbox1.y1,
                }
                annotation = Annotation(
                    document=document,
                    label=label,
                    confidence=span['Accuracy'],
                    label_set=label_set,
                    annotation_set=annotation_set,
                    bboxes=[ann_bbox],
                )
            else:
                annotation = Annotation(
                    document=document,
                    label=label,
                    confidence=span['Accuracy'],
                    label_set=label_set,
                    annotation_set=annotation_set,
                    spans=[Span(start_offset=span['Start'], end_offset=span['End'])],
                )
            if annotation.spans[0].offset_string is None:
                raise NotImplementedError(
                    f"Extracted {annotation} does not have a correspondence in the " f"text of {document}."
                )


def extraction_result_to_document(document: Document, extraction_result: dict) -> Document:
    """Return a virtual Document annotated with AI Model output."""
    virtual_doc = deepcopy(document)
    virtual_annotation_set_id = 0  # counter for across mult. Annotation Set groups of a Label Set

    # define Annotation Set for the Category Label Set: todo: this is unclear from API side
    # default Annotation Set will be always added even if there are no predictions for it
    category_label_set = document.category.project.get_label_set_by_id(document.category.id_)
    virtual_default_annotation_set = AnnotationSet(
        document=virtual_doc, label_set=category_label_set, id_=virtual_annotation_set_id
    )

    for label_or_label_set_name, information in extraction_result.items():
        if isinstance(information, pandas.DataFrame) and not information.empty:
            # annotations belong to the default Annotation Set
            label = document.category.project.get_label_by_name(label_or_label_set_name)
            add_extractions_as_annotations(
                document=virtual_doc,
                extractions=information,
                label=label,
                label_set=category_label_set,
                annotation_set=virtual_default_annotation_set,
            )

        elif isinstance(information, list) or isinstance(information, dict):
            # process multi Annotation Sets that are not part of the category Label Set
            label_set = document.category.project.get_label_set_by_name(label_or_label_set_name)

            if not isinstance(information, list):
                information = [information]

            for entry in information:  # represents one of pot. multiple annotation-sets belonging of one LabelSet
                virtual_annotation_set_id += 1
                virtual_annotation_set = AnnotationSet(
                    document=virtual_doc, label_set=label_set, id_=virtual_annotation_set_id
                )

                for label_name, extractions in entry.items():
                    label = document.category.project.get_label_by_name(label_name)
                    add_extractions_as_annotations(
                        document=virtual_doc,
                        extractions=extractions,
                        label=label,
                        label_set=label_set,
                        annotation_set=virtual_annotation_set,
                    )
    return virtual_doc


class Trainer:
    """Base Model to extract information from unstructured human readable text."""

    def __init__(self, *args, **kwargs):
        """Initialize ExtractionModel."""
        # Go through keyword arguments, and either save their values to our
        # instance, or raise an error.
        self.clf = None
        self.category = None
        self.name = self.__class__.__name__
        self.label_feature_list = None  # will be set later

        self.df_data = None
        self.df_valid = None
        self.df_train = None
        self.df_test = None

        self.X_train = None
        self.y_train = None
        self.X_valid = None
        self.y_valid = None
        self.X_test = None
        self.y_test = None

        self.evaluation = None

    def build(self, **kwargs):
        """Build an ExtractionModel using train valid split."""
        self.create_candidates_dataset()
        self.train_valid_split()
        self.fit()
        self.evaluate()
        self.lose_weight()
        return self

    def name_lower(self):
        """Convert class name to machine readable name."""
        return f'{self.name.lower().strip()}'

    def lose_weight(self):
        """Delete everything that is not necessary for extraction."""
        self.df_valid = None
        self.df_train = None
        self.df_test = None

        self.X_train = None
        self.y_train = None
        self.X_valid = None
        self.y_valid = None
        self.X_test = None
        self.y_test = None

        # TODO what is this?
        self.valid_data = None
        self.training_data = None
        self.test_data = None

        self.df_data_list = None

        for label in self.category.project.labels:
            label.lose_weight()

        for label_set in self.category.label_sets or []:
            label_set.lose_weight()

        logger.info(f'Lose weight was executed on {self.name}')

    # def get_ai_model(self):
    #     """Try to load the latest pickled model."""
    #     try:
    #         return load_pickle(get_latest_document_model(f'*_{self.name_lower()}.pkl'))
    #     except FileNotFoundError:
    #         return None

    def create_candidates_dataset(self):
        """Use as placeholder Function."""
        logger.warning(f'{self} does not train a classifier.')
        pass

    def train_valid_split(self):
        """Use as placeholder Function."""
        logger.warning(f'{self} does not use a valid and train data split.')
        pass

    def fit(self):
        """Use as placeholder Function."""
        logger.warning(f'{self} does not train a classifier.')
        pass

    def fit_label_set_clf(self):
        """Use as placeholder Function."""
        logger.warning(f'{self} does not train a label set classifier.')
        pass

    def evaluate(self):
        """Use as placeholder Function."""
        logger.warning(f'{self} does not evaluate results.')
        pass

    def extract(self, *args, **kwargs):
        """Use as placeholder Function."""
        # todo: extract should return a Document
        #  see https://github.com/konfuzio-ai/konfuzio-sdk/blob/64fd8792/konfuzio_sdk/data.py#L1182
        logger.warning(f'{self} does not extract.')
        pass

    # def clf_info(self, feature_pattern=None, contains=True) -> None:
    #     """
    #     Log info about feature importance after clf is fitted.
    #
    #     Args:
    #     ----
    #         feature_pattern: A string which should be or should not be contained in the name of the feature
    #         contains: Boolean, used to determine if the feature_pattern should be contained or should not be contained
    #
    #     Returns: None
    #
    #     """
    #     try:
    #         infotable = pandas.DataFrame()
    #         infotable['feature'] = self.X_train.columns
    #         infotable['importance'] = self.clf.feature_importances_
    #         if feature_pattern:
    #             infotable_text = tabulate(
    #                 infotable[infotable.feature.str.contains(feature_pattern) == contains].sort_values(
    #                     "importance", ascending=False
    #                 ),
    #                 floatfmt=".5%",
    #                 headers="keys",
    #                 tablefmt="pipe",
    #             )
    #         else:  # return all
    #             infotable_text = tabulate(
    #                 infotable.sort_values("importance", ascending=False),
    #                 floatfmt=".5%",
    #                 headers="keys",
    #                 tablefmt="pipe",
    #             )
    #
    #         logger.info(
    #             f'DOES{"NOT" if not contains else " "} CONTAIN "{feature_pattern}" FEATURE RATING (DESCENDING):'
    #             f'\n{infotable_text}\n'
    #         )
    #     except AttributeError:
    #         logger.exception('.feature_importances_ not available for classifier')
    #
    #     logger.info(f'Size of the classifier is: {sys.getsizeof(self.clf)}')

    def save(self, output_dir: str, include_konfuzio=True):
        """
        Save the label model as bz2 compressed pickle object to the release directory.

        Saving is done by: getting the serialized pickle object (via dill), "optimizing" the serialized object with the
        built-in pickletools.optimize function (see: https://docs.python.org/3/library/pickletools.html), saving the
        optimized serialized object.

        We then compress the pickle file with bz2 using shutil.copyfileobject which writes in chunks to avoid loading
        the entire pickle file in memory.

        Finally, we delete the dill file and are left with the bz2 file which has a .pkl extension.

        :return: Path of the saved model file
        """
        # Keep Documents of the Category so that we can restore them later
        category_documents = self.category.documents() + self.category.test_documents()

        # TODO: add Document.lose_weight in SDK - remove NO_LABEL Annotations from the Documents
        # for document in category_documents:
        #     no_label_annotations = document.annotations(label=self.category.project.no_label)
        #     clean_annotations = list(set(document.annotations()) - set(no_label_annotations))
        #     document._annotations = clean_annotations

        # self.lose_weight() # todo make this optional: otherwise evaluate will not work on self

        from pympler import asizeof

        logger.info(f'Saving model - {asizeof.asizeof(self) / 1_000_000} MB')

        sys.setrecursionlimit(99999999)

        logger.info('Getting save paths')
        import konfuzio_sdk

        if include_konfuzio:
            cloudpickle.register_pickle_by_value(konfuzio_sdk)
            # todo register all dependencies?

        # output_dir = self.category.project.model_folder
        # file_path = os.path.join(output_dir, f'{get_timestamp()}_{self.category.name.lower())}')

        # moke sure output dir exists
        pathlib.Path(output_dir).mkdir(parents=True, exist_ok=True)

        temp_pkl_file_path = os.path.join(output_dir, f'{get_timestamp()}_{self.category.name.lower()}.dill')
        pkl_file_path = os.path.join(output_dir, f'{get_timestamp()}_{self.category.name.lower()}.pkl')

        logger.info('Saving model with dill')
        # first save with dill
        with open(temp_pkl_file_path, 'wb') as f:  # see: https://stackoverflow.com/a/9519016/5344492
            cloudpickle.dump(self, f)

        logger.info('Compressing model with bz2')

        # then save to bz2 in chunks
        with open(temp_pkl_file_path, 'rb') as input_f:
            with bz2.open(pkl_file_path, 'wb') as output_f:
                shutil.copyfileobj(input_f, output_f)

        logger.info('Deleting dill file')
        # then delete dill file
        os.remove(temp_pkl_file_path)

        size_string = f'{os.path.getsize(pkl_file_path) / 1_000_000} MB'
        logger.info(f'Model ({size_string}) {self.name_lower()} was saved to {pkl_file_path}')

        # restore Documents of the Category so that we can run the evaluation later
        self.category.project._documents = category_documents

        return pkl_file_path

    # def update(self, instance):
    #     """
    #     Add an extraction model instance to self. Deletes models with the same name before.
    #
    #     :param instance:
    #     :return:
    #     """
    #     for i, label in enumerate(self.labels):
    #         if instance.name == label.name:
    #             logger.info(f'Delete old label {label.name} before adding new one...')
    #             del self.labels[i]
    #
    #     self.add(instance)

    # def add(self, instance):
    #     """
    #     Add an extraction model instance to self.
    #
    #     :param instance: An inherited ExtractionModel, i.e. LabelExtractionModels or CategoryExtractionModels
    #     :return:
    #     """
    #     from konfuzio.models_classification import CategoryExtractionModel
    #     from konfuzio.models_legacy import (
    #         LabelExtractionModel,
    #         PatternExtractionModel,
    #         MultiLabelExtractionModel,
    #         LabelAnnotationModel,
    #         DocumentExtractionModel,
    #     )
    #
    #     add_success_info = f'Document {self.name} now also extracts {instance.name}.'
    #
    #     for i, label in enumerate(self.labels):
    #         if instance.name == label.name:
    #             logger.error(f'{label.name} does already exist as label. You may want to use update().')
    #             raise Exception('Exiting. See log for details...')
    #
    #     if (
    #         isinstance(instance, Label)
    #         or isinstance(instance, PatternExtractionModel)
    #         or isinstance(instance, MultiLabelExtractionModel)
    #         or isinstance(instance, LabelAnnotationModel)
    #         or isinstance(instance, LabelExtractionModel)
    #     ):
    #         self.labels.append(instance)
    #         logger.info(add_success_info)
    #     elif isinstance(instance, CategoryExtractionModel):
    #         self.categories.append(instance)
    #         logger.info(add_success_info)
    #     elif isinstance(instance, DocumentExtractionModel):
    #         self.documents.append(instance)
    #         logger.info(add_success_info)
    #     else:
    #         raise Exception(f'{instance.name} cannot be added to document {self.name}.')


class GroupAnnotationSets:
    """Groups Annotation into Annotation Sets."""

    def __init__(self):
        """Initialize TemplateClf."""
        self.n_nearest_template = 5
        self.max_depth = 100
        self.n_estimators = 100

    def fit_template_clf(self) -> Tuple[Optional[object], Optional[List['str']]]:
        """
        Fit classifier to predict start lines of Sections.

        :param documents:
        :return:
        """
        # Only train template clf is there are non default templates
        self.section_labels = self.category.label_sets  # todo what is it?
        if not [lset for lset in self.category.label_sets if not lset.is_default]:
            # todo see https://gitlab.com/konfuzio/objectives/-/issues/2247
            # todo check for NO_LABEL_SET if we should keep it
            return
        logger.info('Start training of Multi-class Label Set Classifier.')
        # ignores the section count as it actually worsens results
        # todo check if no category labels should be ignored
        self.template_feature_list = [label.name for label in self.category.project.labels]
        n_nearest = self.n_nearest_template if hasattr(self, 'n_nearest_template') else 0

        # Pretty long feature generation
        df_train_label = self.df_train
        # df_valid_label = self.df_valid
        df_valid_label_list = []  # todo why?

        df_train_label_list = [(document_id, df_doc) for document_id, df_doc in df_train_label.groupby('document_id')]

        df_train_template_list = []
        df_train_ground_truth_list = []
        for document_id, df_doc in df_train_label_list:
            document = self.category.project.get_document_by_id(document_id)
            # Train classifier only on documents with a matching document template.
            if (
                hasattr(self, 'default_section_label')
                and self.default_section_label
                and self.default_section_label != document.category_template
            ):
                logger.info(f'Skip document {document} because its template does not match.')
                continue
            df_train_template_list.append(self.convert_label_features_to_template_features(df_doc, document.text))
            df_train_ground_truth_list.append(self.build_document_template_feature(document))

        df_valid_template_list = []
        df_valid_ground_truth_list = []
        for document_id, df_doc in df_valid_label_list:
            document = self._get_document(document_id)
            if (
                hasattr(self, 'default_section_label')
                and self.default_section_label
                and self.default_section_label != document.category_template
            ):
                logger.info(f'Skip document {document} because its template does not match.')
                continue
            df_valid_template_list.append(self.convert_label_features_to_template_features(df_doc, document.text))
            df_valid_ground_truth_list.append(self.build_document_template_feature(document))

        df_train_expanded_features_list = [
            self.generate_relative_line_features(n_nearest, pandas.DataFrame(df, columns=self.template_feature_list))
            for df in df_train_template_list
        ]
        df_valid_expanded_features_list = [
            self.generate_relative_line_features(n_nearest, pandas.DataFrame(df, columns=self.template_feature_list))
            for df in df_valid_template_list
        ]

        df_train_ground_truth = pandas.DataFrame(
            pandas.concat(df_train_ground_truth_list), columns=self.template_feature_list + ['y']
        )
        if len(df_valid_expanded_features_list) > 0:
            df_valid_ground_truth = pandas.DataFrame(
                pandas.concat(df_valid_ground_truth_list), columns=self.template_feature_list + ['y']
            )

        self.template_expanded_feature_list = list(df_train_expanded_features_list[0].columns)

        df_train_expanded_features = pandas.DataFrame(
            pandas.concat(df_train_expanded_features_list), columns=self.template_expanded_feature_list
        )
        if len(df_valid_expanded_features_list) > 0:
            df_valid_expanded_features = pandas.DataFrame(
                pandas.concat(df_valid_expanded_features_list), columns=self.template_expanded_feature_list
            )

        y_train = numpy.array(df_train_ground_truth['y']).astype('str')
        x_train = df_train_expanded_features[self.template_expanded_feature_list]

        if len(df_valid_expanded_features_list) > 0:
            y_valid = numpy.array(df_valid_ground_truth['y']).astype('str')
            x_valid = df_valid_expanded_features[self.template_expanded_feature_list]

        # fillna(0) is used here as not every label is found in every document at least once
        x_train.fillna(0, inplace=True)

        if len(df_valid_expanded_features_list) > 0:
            x_valid.fillna(0, inplace=True)

        # No features available
        if x_train.empty:
            logger.error(
                'No features available to train template classifier, ' 'probably because there are no annotations.'
            )
            return None, None

        clf = RandomForestClassifier(n_estimators=self.n_estimators, max_depth=self.max_depth, random_state=420)
        clf.fit(x_train, y_train)

        if len(df_valid_expanded_features_list) > 0:
            y_pred = clf.predict(x_valid)
            # evaluate the clf
            self.evaluate_template_clf(y_valid, y_pred, clf.classes_)

        self.template_clf = clf
        return self.template_clf, self.template_feature_list

    # def _get_document(self, document_id):
    #     """Return the document text for a specific document_id."""
    #     for document in self.documents:
    #         if document.id_ == document_id:
    #             return document
    #
    #     logger.error('No document fitting this document_id: ' + str(document_id) + ' found!')
    #     return None

    def generate_relative_line_features(self, n_nearest: int, df_features: pandas.DataFrame) -> pandas.DataFrame:
        """Add the features of the n_nearest previous and next lines."""
        if n_nearest == 0:
            return df_features

        min_row = 0
        max_row = len(df_features.index) - 1

        df_features_new_list = []

        for index, row in df_features.iterrows():
            row_dict = row.to_dict()

            # get a relevant lines and add them to the dict_list
            for i in range(n_nearest):
                if index + (i + 1) <= max_row:
                    d_next = df_features.iloc[index + (i + 1)].to_dict()
                else:
                    d_next = row.to_dict()
                    d_next = {k: 0 for k, v in d_next.items()}
                d_next = {f'next_line_{i + 1}_{k}': v for k, v in d_next.items()}

                if index - (i + 1) >= min_row:
                    d_prev = df_features.iloc[index - (i + 1)].to_dict()
                else:
                    d_prev = row.to_dict()
                    d_prev = {k: 0 for k, v in d_prev.items()}
                d_prev = {f'prev_line_{i + 1}_{k}': v for k, v in d_prev.items()}
                # merge the line into the row dict
                row_dict = {**row_dict, **d_next, **d_prev}

            df_features_new_list.append(row_dict)

        return pandas.DataFrame(df_features_new_list)

    def convert_label_features_to_template_features(
        self, feature_df_label: pandas.DataFrame, document_text
    ) -> pandas.DataFrame:
        """
        Convert the feature_df for the label_clf to a feature_df for the template_clf.

        The input is the Feature-Dataframe and text for one document.
        """
        # reset indices to avoid bugs with stupid NaN's as label_text
        feature_df_label.reset_index(drop=True, inplace=True)

        # predict and transform the DataFrame to be compatible with the other functions
        results = pandas.DataFrame(
            data=self.clf.predict_proba(X=feature_df_label[self.label_feature_list]), columns=self.clf.classes_
        )

        # Remove no_label predictions
        if 'NO_LABEL' in results.columns:
            results = results.drop(['NO_LABEL'], axis=1)

        # Store most likely prediction and its accuracy in separated columns
        feature_df_label['label_text'] = results.idxmax(axis=1)
        feature_df_label['Accuracy'] = results.max(axis=1)

        # Do column renaming to be compatible with text-annotation
        feature_df_label.rename(
            columns={
                'start_offset': 'Start',
                'end_offset': 'End',
                'offset_string': 'Candidate',
                'regex': 'Regex',
                'threshold': 'OptimalThreshold',
            },
            inplace=True,
        )
        feature_df_label['Translated_Candidate'] = feature_df_label['Candidate']
        feature_df_label['label'] = feature_df_label['label_text']

        # convert the transformed df to the new template features
        feature_df_template = self.build_document_template_feature_X(document_text, feature_df_label).filter(
            self.template_feature_list, axis=1
        )
        feature_df_template = feature_df_template.reindex(columns=self.template_feature_list).fillna(0)

        return feature_df_template

    # def evaluate_template_clf(self, y_true, y_pred, classes):
    #     """
    #     Evaluate a template clf by comparing the ground truth to the predictions.
    #
    #     Classes are the different classes of the template clf (the different sections).
    #     """
    #     logger.info('Evaluate template classifier on the validation data.')
    #
    #     try:
    #         matrix = pandas.DataFrame(
    #             confusion_matrix(y_true=y_true, y_pred=y_pred, labels=classes),
    #             columns=classes,
    #             index=['y_true_' + x for x in classes],
    #         )
    #         logger.info('\n' + tabulate(matrix, headers=classes))
    #     except ValueError:
    #         pass
    #     logger.info(f'precision: {precision_score(y_true, y_pred, average="micro")}')
    #     logger.info(f'recall: {recall_score(y_true, y_pred, average="micro")}')

    def build_document_template_feature(self, document) -> pandas.DataFrame():
        """Build document feature for template classifier given ground truth."""
        df = pandas.DataFrame()
        char_count = 0

        document_annotations = [
            annotation for annotation_set in document.annotation_sets() for annotation in annotation_set.annotations
        ]

        # Loop over lines
        for i, line in enumerate(document.text.replace('\f', '\n').split('\n')):
            matched_section = None
            new_char_count = char_count + len(line)
            assert line == document.text[char_count:new_char_count]
            # TODO: Currently we can't handle
            for section in document.annotation_sets():
                if section.start_offset and char_count <= section.start_offset < new_char_count:
                    matched_section: AnnotationSet = section
                    break

            line_annotations = [
                x for x in document_annotations if char_count <= x.spans[0].start_offset < new_char_count
            ]
            annotations_dict = dict((x.label.name, True) for x in line_annotations)
            counter_dict = dict(
                collections.Counter(annotation.annotation_set.label_set.name for annotation in line_annotations)
            )
            y = matched_section.label_set.name if matched_section else 'No'
            tmp_df = pandas.DataFrame(
                [{'line': i, 'y': y, 'document': document.id_, **annotations_dict, **counter_dict}]
            )
            df = pandas.concat([df, tmp_df], ignore_index=True)
            char_count = new_char_count + 1
        df['text'] = document.text.replace('\f', '\n').split('\n')
        return df.fillna(0)

    def build_document_template_feature_X(self, text, df) -> pandas.DataFrame():
        """
        Calculate features for a document given the extraction results.

        :param text:
        :param df:
        :return:
        """
        if self.category is None:
            raise AttributeError(f'{self} does not provide a Category.')

        global_df = pandas.DataFrame()
        char_count = 0
        # Using OptimalThreshold is a bad idea as it might defer between training (actual treshold from the label)
        # and runtime (default treshold.
        df = df[df['Accuracy'] >= 0.1]  # df['OptimalThreshold']]
        for i, line in enumerate(text.replace('\f', '\n').split('\n')):
            new_char_count = char_count + len(line)
            assert line == text[char_count:new_char_count]
            line_df = df[(char_count <= df['Start']) & (df['End'] <= new_char_count)]
            annotations = [row for index, row in line_df.iterrows()]
            annotations_dict = dict((x['label'], True) for x in annotations)
            counter_dict = {}
            # annotations_accuracy_dict = defaultdict(lambda: 0)
            for annotation in annotations:
                # annotations_accuracy_dict[f'{annotation["label"]}_accuracy'] += annotation['Accuracy']
                try:
                    label = next(x for x in self.category.project.labels if x.name == annotation['label'])
                except StopIteration:
                    continue
                for section_label in self.section_labels:
                    if label in section_label.labels:
                        if section_label.name in counter_dict.keys():
                            counter_dict[section_label.name] += 1
                        else:
                            counter_dict[section_label.name] = 1
            tmp_df = pandas.DataFrame([{**annotations_dict, **counter_dict}])
            global_df = pandas.concat([global_df, tmp_df], ignore_index=True)
            char_count = new_char_count + 1
        global_df['text'] = text.replace('\f', '\n').split('\n')
        return global_df.fillna(0)

    def extract_template_with_clf(self, text, res_dict):
        """Run template classifier to calculate sections."""
        logger.info('Extract sections.')
        n_nearest = self.n_nearest_template if hasattr(self, 'n_nearest_template') else 0
        feature_df = self.build_document_template_feature_X(text, dict_to_dataframe(res_dict)).filter(
            self.template_feature_list, axis=1
        )
        feature_df = feature_df.reindex(columns=self.template_feature_list).fillna(0)
        feature_df = self.generate_relative_line_features(n_nearest, feature_df)

        res_series = self.template_clf.predict(feature_df)
        res_templates = pandas.DataFrame(res_series)
        # res_templates['text'] = text.replace('\f', '\n').split('\n')  # Debug code.

        # TODO improve ordering. What happens if Annotations are not matched?
        logger.info('Building new res dict')
        new_res_dict = {}
        text_replaced = text.replace('\f', '\n')

        # Add extractions from non-default sections.
        for section_label in [x for x in self.section_labels if not x.is_default]:
            # Add Extraction from SectionLabels with multiple sections (as list).
            if section_label.has_multiple_annotation_sets:
                new_res_dict[section_label.name] = []
                detected_sections = res_templates[res_templates[0] == section_label.name]
                # List of tuples, e.g. [(1, DefaultSectionName), (14, DetailedSectionName), ...]
                # line_list = [(index, row[0]) for index, row in detected_sections.iterrows()]
                if not detected_sections.empty:
                    i = 0
                    # for each line of a certain section label
                    for line_number, section_name in detected_sections.iterrows():
                        section_dict = {}
                        # we try to find the labels that match that section
                        for label in section_label.labels:
                            if label.name in res_dict.keys():
                                label_df = res_dict[label.name]
                                if label_df.empty:
                                    continue
                                # todo: the next line is memory heavy
                                #  https://gitlab.com/konfuzio/objectives/-/issues/9342
                                label_df['line'] = (
                                    label_df['Start'].apply(lambda x: text_replaced[: int(x)]).str.count('\n')
                                )
                                try:
                                    next_section_start: int = detected_sections.index[i + 1]  # line_list[i + 1][0]
                                except Exception:
                                    next_section_start: int = text_replaced.count('\n') + 1

                                # we get the label df that is contained within the section
                                label_df = label_df[
                                    (line_number <= label_df['line']) & (label_df['line'] < next_section_start)
                                ]
                                if label_df.empty:
                                    continue
                                section_dict[label.name] = label_df  # Add to new result dict
                                # Remove from input dict
                                res_dict[label.name] = res_dict[label.name].drop(label_df.index)
                        i += 1
                        new_res_dict[section_label.name].append(section_dict)
            # Add Extraction from SectionLabels with single section (as dict).
            else:
                _dict = {}
                for label in section_label.labels:
                    if label.name in res_dict.keys():
                        _dict[label.name] = res_dict[label.name]
                        del res_dict[label.name]
                if _dict:
                    new_res_dict[section_label.name] = _dict
                continue

        # Finally add remaining extractions to default section (if they are allowed to be there).
        for section_label in [x for x in self.section_labels if x.is_default]:
            for label in section_label.labels:
                if label.name in res_dict.keys():
                    new_res_dict[label.name] = res_dict[label.name]
                    del res_dict[label.name]
            continue

        return new_res_dict


class DocumentAnnotationMultiClassModel(Trainer, GroupAnnotationSets):
    """Encode visual and textual features to extract text regions.

    Fit a extraction pipeline to extract linked Annotations.

    Both Label and Label Set classifiers are using a RandomForestClassifier from scikit-learn to run in a low memory and
    single CPU environment. A random forest classifier is a group of decision trees classifiers, see:
    https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.RandomForestClassifier.html

    The parameters of this class allow to select the Tokenizer, to configure the Label and Label Set classifiers and to
    select the type of features used by the Label and Label Set classifiers.

    They are divided in:
    - tokenizer selection
    - parametrization of the Label classifier
    - parametrization of the Label Set classifier
    - features for the Label classifier
    - features for the Label Set classifier

    By default, the text of the Documents is split into smaller chunks of text based on whitespaces
    ('tokenizer_whitespace'). That means that all words present in the text will be shown to the AI. It is possible to
    define if the splitting of the text into smaller chunks should be done based on regexes learned from the
    Spans of the Annotations of the Category ('tokenizer_regex') or if to use a model from Spacy library for German
    language ('tokenizer_spacy'). Another option is to use a pre-defined list of tokenizers based on regexes
    ('tokenizer_regex_list') and, on top of the pre-defined list, to create tokenizers that match what is missed
    by those ('tokenizer_regex_combination').

    Some parameters of the scikit-learn RandomForestClassifier used for the Label and/or Label Set classifier
    can be set directly in Konfuzio Server ('label_n_estimators', 'label_max_depth', 'label_class_weight',
    'label_random_state', 'label_set_n_estimators', 'label_set_max_depth').

    Features are measurable pieces of data of the Annotation. By default, a combination of features is used that
    includes features built from the text of the Annotation ('string_features'), features built from the position of
    the Annotation in the Document ('spatial_features') and features from the Spans created by a WhitespaceTokenizer on
    the left or on the right of the Annotation ('n_nearest_left', 'n_nearest_right', 'n_nearest_across_lines).
    It is possible to exclude any of them ('spatial_features', 'string_features', 'n_nearest_left', 'n_nearest_right')
    or to specify the number of Spans created by a WhitespaceTokenizer to consider
    ('n_nearest_left', 'n_nearest_right').

    While extracting, the Label Set classifier takes the predictions from the Label classifier as input.
    The Label Set classifier groups them intoAnnotation sets.
    It is possible to define the confidence threshold for the predictions to be considered by the
    Label Set classifier ('label_set_confidence_threshold'). However, the label_set_confidence_threshold is not applied
    to the final predictions of the Extraction AI.
    """

    def __init__(
        self,
        n_nearest: int = 2,
        first_word: bool = True,
        n_estimators: int = 100,
        max_depth: int = 100,
        no_label_limit: Union[int, float, None] = None,
        n_nearest_across_lines: bool = False,
        *args,
        **kwargs,
    ):
        """DocumentAnnotationModel."""
        super().__init__(*args, **kwargs)
        GroupAnnotationSets.__init__(self)
        # self.label_list = None
        self.label_feature_list = None

        # If this is True use the generic regex generator
        self.use_generic_regex = True
        self.category: Category = None
        self.n_nearest = n_nearest
        self.first_word = first_word
        self.max_depth = max_depth
        self.n_estimators = n_estimators
        self.no_label_limit = no_label_limit
        self.n_nearest_across_lines = n_nearest_across_lines
        self.train_split_percentage = None

        self.substring_features = kwargs.get('substring_features', None)
        self.catchphrase_features = kwargs.get('catchphrase_features', None)

        self.regexes = None  # set later
        self.tokenizer = None

    def features(self, document: Document):
        """Calculate features using the best working default values that can be overwritten with self values."""
        df, _feature_list, _temp_df_raw_errors = process_document_data(
            document=document,
            spans=document.spans(use_correct=False),
            n_nearest=self.n_nearest if hasattr(self, 'n_nearest') else 2,
            first_word=self.first_word if hasattr(self, 'first_word') else True,
            tokenize_fn=self.tokenizer.tokenize,  # todo: we are tokenizing the document multiple times
            catchphrase_list=self.catchphrase_features if hasattr(self, 'catchphrase_features') else None,
            substring_features=self.substring_features if hasattr(self, 'substring_features') else None,
            n_nearest_across_lines=self.n_nearest_across_lines if hasattr(self, 'n_nearest_across_lines') else False,
        )
        return df, _feature_list, _temp_df_raw_errors

    def extract(self, document: Document) -> 'Dict':
        """
        Infer information from a given Document.

        :param text: HTML or raw text of document
        :param bbox: Bbox of the document
        :return: dictionary of labels and top candidates

        :raises:
         AttributeError: When missing a Tokenizer
         NotFittedError: When CLF is not fitted

        """
        if self.tokenizer is None:
            raise AttributeError(f'{self} missing Tokenizer.')

        if self.clf is None and hasattr(self, 'label_clf'):  # Can be removed for models after 09.10.2020
            self.clf = self.label_clf

        if self.clf is None:
            raise AttributeError(f'{self} does not provide a Label Classifier. Please add it.')
        else:
            check_is_fitted(self.clf)

        # Main Logic -------------------------
        # 1. start inference with new document
        inference_document = deepcopy(document)
        # 2. tokenize
        self.tokenizer.tokenize(inference_document)
        if not inference_document.spans():
            logger.error(f'{self.tokenizer} does not provide Spans for {document}')
            raise NotImplementedError('No error handling when Spans are missing.')
        # 3. preprocessing
        df, _feature_names, _raw_errors = self.features(inference_document)
        try:
            independet_variables = df[self.label_feature_list]
        except KeyError:
            raise KeyError(f'Features of {document} do not match the features of the pipeline.')
            # todo calculate features of Document as defined in pipeline and do not check afterwards
        # 4. prediction and store most likely prediction and its accuracy in separated columns
        results = pandas.DataFrame(data=self.clf.predict_proba(X=independet_variables), columns=self.clf.classes_)

        # Remove no_label predictions
        if 'NO_LABEL' in results.columns:
            results = results.drop(['NO_LABEL'], axis=1)

        if 'NO_LABEL_SET' in results.columns:
            results = results.drop(['NO_LABEL_SET'], axis=1)

        df['label_text'] = results.idxmax(axis=1)
        df['Accuracy'] = results.max(axis=1)
        # 5. Translation
        df['Translated_Candidate'] = df['offset_string']  # todo: make translation explicit: It's a cool Feature
        # Main Logic -------------------------

        # Do column renaming to be compatible with text-annotation
        # todo: how can multilines be created via SDK
        # todo: why do we need to adjust the woring for Server?
        # todo: which other attributes could be send in the extraction method?
        df.rename(
            columns={
                'start_offset': 'Start',
                'end_offset': 'End',
                'page_index': 'page_index',
                'offset_string': 'Candidate',
                'regex': 'Regex',
                'threshold': 'OptimalThreshold',
            },
            inplace=True,
        )

        # Convert DataFrame to Dict with labels as keys and label dataframes as value.
        res_dict = {}
        for label_text in set(df['label_text']):
            label_df = df[df['label_text'] == label_text].copy()
            if not label_df.empty:
                res_dict[label_text] = label_df

        # Filter results that are bellow the extract threshold
        # (helpful to reduce the size in case of many predictions/ big documents)
        if hasattr(self, 'extract_threshold') and self.extract_threshold is not None:
            logger.info('Filtering res_dict')
            for label, value in res_dict.items():
                if isinstance(value, pandas.DataFrame):
                    res_dict[label] = value[value['Accuracy'] > self.extract_threshold]

        # Try to calculate sections based on template classifier.
        if hasattr(self, 'template_clf'):  # todo smarter handling of multiple clf
            res_dict = self.extract_template_with_clf(inference_document.text, res_dict)

        # place annotations back
        # document._annotations = doc_annotations

        return res_dict

    # def create_candidates_dataset(self, *args, **kwargs):
    #     """
    #     Build DocumentAnnotation Model for this project.
    #
    #     :param klass: Custom DocumentAnnotationModel e.g. Invoice(DocumentAnnotationModel)
    #     :return: path to the pickled document model as str
    #     """
    #     if self.use_generic_regex:
    #         for label in self.labels:
    #             label.regex(multiprocessing=self.multiprocessing)
    #         self.regexes = [regex for label in self.labels for regex in label.regex()]
    #     # Use default entity generating regex if there a no regexes at hand.
    #     else:
    #         self.regexes = ['[^ \n\t\f]+']
    #
    #     if not hasattr(self, 'no_label_limit'):
    #         self.no_label_limit = None
    #
    #     self.df_train, self.label_feature_list = self.feature_function(
    #         documents=self.documents, no_label_limit=self.no_label_limit
    #     )
    #
    #     if self.df_train.empty:
    #         logger.warning('df_train is empty! No training data found.')
    #         return None
    #
    #     self.df_test, test_label_feature_list = self.feature_function(
    #         documents=self.test_documents, no_label_limit=self.no_label_limit
    #     )
    #
    #     if not self.df_test.empty:
    #         assert self.label_feature_list == test_label_feature_list
    #
    #     # updates the label_feature_list according to the outdated_feature_list
    #     outdated_features_list = ['feat_date_count']
    #     for outdated_feature in outdated_features_list:
    #         self.label_feature_list = [feat for feat in self.label_feature_list if outdated_feature not in feat]
    #
    #     return self

    def lose_weight(self):
        """Lose weight before pickling."""
        super().lose_weight()

        # remove documents
        self.documents = None
        self.test_documents = None

    def label_train_document(self, virtual_document: Document, original_document: Document):
        """Assign labels to Annotations in newly tokenized virtual training document."""
        doc_spans = original_document.spans(use_correct=True)
        s_i = 0
        for span in virtual_document.spans():
            while s_i < len(doc_spans) and span.start_offset > doc_spans[s_i].end_offset:
                s_i += 1
            if s_i >= len(doc_spans):
                break
            if span.end_offset < doc_spans[s_i].start_offset:
                continue

            r = range(doc_spans[s_i].start_offset, doc_spans[s_i].end_offset + 1)
            if span.start_offset in r and span.end_offset in r:
                span.annotation.label = doc_spans[s_i].annotation.label

    def feature_function(
        self,
        documents: List[Document],
        no_label_limit=None,
        retokenize=True,
        require_revised_annotations=False,
    ) -> Tuple[List[pandas.DataFrame], list]:
        """Calculate features per Span of Annotations.

        :param documents: List of documents to extract features from.
        :param no_label_limit: Int or Float to limit number of new annotations to create during tokenization.
        :param retokenize: Bool for whether to recreate annotations from scratch or use already existing annotations.
        :return: Dataframe of features and list of feature names.
        """
        logger.info('Start generating features.')
        df_real_list = []
        df_raw_errors_list = []
        feature_list = []

        # todo make regex Tokenizer optional as those will be saved by the Server
        # if not hasattr(self, 'regexes'):  # Can be removed for models after 09.10.2020
        #    self.regexes = [regex for label_model in self.labels for regex in label_model.label.regex()]

        for document in documents:
            # todo check for tokenizer: self.tokenizer.tokenize(document)  # todo: do we need it?
            # todo check removed  if x.x0 and x.y0
            # todo: use NO_LABEL for any Annotation that has no Label, instead of keeping Label = None
            for span in document.spans(use_correct=False):
                if span.annotation.id_:
                    # Annotation
                    # we use "<" below because we don't want to have unconfirmed annotations in the training set,
                    # and the ones below threshold wouldn't be considered anyway
                    if (
                        span.annotation.is_correct
                        or (not span.annotation.is_correct and span.annotation.revised)
                        or (
                            span.annotation.confidence
                            and hasattr(span.annotation.label, 'threshold')
                            and span.annotation.confidence < span.annotation.label.threshold
                        )
                    ):
                        pass
                    else:
                        if require_revised_annotations:
                            raise ValueError(
                                f"{span.annotation} is unrevised in this dataset and can't be used for training!"
                                f"Please revise it manually by either confirming it, rejecting it, or modifying it."
                            )
                        else:
                            logger.error(
                                f"{span.annotation} is unrevised in this dataset and may impact model "
                                f"performance! Please revise it manually by either confirming it, rejecting "
                                f"it, or modifying it."
                            )

            if retokenize:
                virt_document = deepcopy(document)
                self.tokenizer.tokenize(virt_document)
                self.label_train_document(virt_document, document)
                document = virt_document
            else:
                self.tokenizer.tokenize(document)

            no_label_annotations = document.annotations(use_correct=False, label=document.project.no_label)
            label_annotations = [x for x in document.annotations(use_correct=False) if x.label.id_ is not None]

            # We calculate features of documents as long as they have IDs, even if they are offline.
            # The assumption is that if they have an ID, then the data came either from the API or from the DB.
            if document.id_ is None and document.copy_of_id is None:
                # inference time todo reduce shuffled complexity
                assert (
                    not label_annotations
                ), "Documents that don't come from the server have no human revised Annotations."
                raise NotImplementedError(
                    f'{document} does not come from the server, please use process_document_data function.'
                )
            else:
                # training time: todo reduce shuffled complexity
                if isinstance(no_label_limit, int):
                    n_no_labels = no_label_limit
                elif isinstance(no_label_limit, float):
                    n_no_labels = int(len(label_annotations) * no_label_limit)
                else:
                    assert no_label_limit is None

                if no_label_limit is not None:
                    no_label_annotations = self.get_best_no_label_annotations(
                        n_no_labels, label_annotations, no_label_annotations
                    )
                    logger.info(
                        f'Document {document} NO_LABEL annotations has been reduced to {len(no_label_annotations)}'
                    )

            logger.info(f'Document {document} has {len(label_annotations)} labeled annotations')
            logger.info(f'Document {document} has {len(no_label_annotations)} NO_LABEL annotations')

            # todo: check if eq method of Annotation prevents duplicates
            # annotations = self._filter_annotations_for_duplicates(label_annotations + no_label_annotations)

            t0 = time.monotonic()

            temp_df_real, _feature_list, temp_df_raw_errors = self.features(document)

            logger.info(f'Document {document} processed in {time.monotonic() - t0:.1f} seconds.')

            feature_list += _feature_list
            df_real_list.append(temp_df_real)
            df_raw_errors_list.append(temp_df_raw_errors)

        feature_list = list(dict.fromkeys(feature_list))  # remove duplicates while maintaining order

        if df_real_list:
            df_real_list = pandas.concat(df_real_list).reset_index(drop=True)
        else:
            raise NotImplementedError  # = pandas.DataFrame()

        return df_real_list, feature_list

    # def get_best_no_label_annotations(
    #     self, n_no_labels: int, label_annotations: List[Annotation], no_label_annotations: List[Annotation]
    # ) -> List[Annotation]:
    #     """Select no_label annotations which are probably most beneficial for training."""
    #     # store our chosen "best" NO_LABELS
    #     best_no_label_annotations = []
    #
    #     # get all the real label offset strings and offsets
    #     label_texts = set([a.offset_string for a in label_annotations])
    #     offsets = set([(a.start_offset, a.end_offset) for a in label_annotations])
    #
    #     _no_label_annotations = []
    #
    #     random.shuffle(no_label_annotations)
    #
    #     # for every NO_LABEL that has an exact string match (but not an offset match)
    #     # to a real label, we add it to the best_no_label_annotations
    #     for annotation in no_label_annotations:
    #         offset_string = annotation.offset_string
    #         start_offset = annotation.start_offset
    #         end_offset = annotation.end_offset
    #         if offset_string in label_texts and (start_offset, end_offset) not in offsets:
    #             best_no_label_annotations.append(annotation)
    #         else:
    #             _no_label_annotations.append(annotation)
    #
    #     # if we have enough NO_LABELS, we stop here
    #     if len(best_no_label_annotations) >= n_no_labels:
    #         return best_no_label_annotations[:n_no_labels]
    #
    #     no_label_annotations = _no_label_annotations
    #     _no_label_annotations = collections.defaultdict(list)
    #
    #     # if we didn't have enough exact matches then we want our NO_LABELS to be the same
    #     # data_type as our real labels
    #     # we count the amount of each data_type in the real labels
    #     # then calculate how many NO_LABEL of each data_type we need
    #     data_type_count = collections.Counter()
    #     data_type_count.update([a.label.data_type for a in label_annotations])
    #     for data_type, count in data_type_count.items():
    #         data_type_count[data_type] = n_no_labels * count / len(label_annotations)
    #
    #     random.shuffle(no_label_annotations)
    #
    #     # we now loop through the NO_LABELS that weren't exact matches and add them to
    #     # the _no_label_annotations dict if we still need more of that data_type
    #     # any that belong to a different data_type are added under the 'extra' key
    #     for annotation in no_label_annotations:
    #         data_type = self.predict_data_type(annotation)
    #         if data_type in data_type_count:
    #             if len(_no_label_annotations[data_type]) < data_type_count[data_type]:
    #                 _no_label_annotations[data_type].append(annotation)
    #             else:
    #                 _no_label_annotations['extra'].append(annotation)
    #         else:
    #             _no_label_annotations['extra'].append(annotation)
    #
    #     # we now add the NO_LABEL annotations with the desired data_type to our
    #     # "best" NO_LABELS
    #     for data_type, _ in data_type_count.most_common():
    #         best_no_label_annotations.extend(_no_label_annotations[data_type])
    #
    #     random.shuffle(best_no_label_annotations)
    #
    #     if len(best_no_label_annotations) >= n_no_labels:
    #         return best_no_label_annotations[:n_no_labels]
    #
    #     # if we still didn't have enough we append the 'extra' NO_LABEL annotations here
    #     best_no_label_annotations.extend(_no_label_annotations['extra'])
    #
    #     # we don't shuffle before we trim the array here so the 'extra' NO_LABEL annotations
    #     # are the ones being cut off at the end
    #     return best_no_label_annotations[:n_no_labels]
    #
    # def train_valid_split(self):
    #     """Split documents randomly into valid and train data."""
    #     logger.info('Splitting into train and valid')
    #
    #     logger.info('Setting NO_LABEL in df_train')
    #     self.df_train.loc[~self.df_train.is_correct, 'label_text'] = 'NO_LABEL'
    #
    #     # if we don't want to split into train/valid then set df_valid to empty df
    #     if self.train_split_percentage == 1:
    #         self.df_valid = pandas.DataFrame()
    #     else:
    #         # else, first find labels which only appear once so can't be stratified
    #         single_labels = [lbl for (lbl, cnt) in self.df_train['label_text'].value_counts().items() if cnt <= 1]
    #         if single_labels:
    #             # if we find any, add to df_singles df
    #             logger.info(f'Following labels appear only once in df_train so are not in df_valid: {single_labels}')
    #             df_singles = self.df_train.groupby('label_text').filter(lambda x: len(x) == 1)
    #
    #         # drop labels that only appear once in df_train as they cannot be stratified
    #         self.df_train = self.df_train.groupby('label_text').filter(lambda x: len(x) > 1)
    #
    #         # do stratified split
    #         self.df_train, self.df_valid = train_test_split(
    #             self.df_train,
    #             train_size=self.train_split_percentage,
    #             stratify=self.df_train['label_text'],
    #             random_state=1,
    #         )
    #
    #         # if we found any single labels, add them back to df_train
    #         if single_labels:
    #             self.df_train = pandas.concat([self.df_train, df_singles])
    #
    #     if self.df_train.empty:
    #         raise Exception('Not enough data to train an AI model.')
    #
    #     if self.df_train[self.label_feature_list].isnull().values.any():
    #         raise Exception('Sample with NaN within the training data found! Check code!')
    #
    #     if not self.df_valid.empty:
    #         if self.df_valid[self.label_feature_list].isnull().values.any():
    #             raise Exception('Sample with NaN within the validation data found! Check code!')

    def fit(self) -> RandomForestClassifier:
        """Given training data and the feature list this function returns the trained regression model."""
        logger.info('Start training of Multi-class Label Classifier.')

        # balanced gives every label the same weight so that the sample_number doesn't effect the results
        self.clf = RandomForestClassifier(
            class_weight="balanced", n_estimators=self.n_estimators, max_depth=self.max_depth, random_state=420
        )

        self.clf.fit(self.df_train[self.label_feature_list], self.df_train['label_text'])

        self.fit_template_clf()

        return self.clf

    def evaluate_full(self, strict: bool = True) -> Evaluation:
        """Evaluate the full pipeline on the pipeline's Test Documents."""
        eval_list = []
        for document in self.test_documents:
            extraction_result = self.extract(document=document)
            predicted_doc = extraction_result_to_document(document, extraction_result)
            eval_list.append((document, predicted_doc))

        self.evaluation = Evaluation(eval_list, strict=strict)

        return self.evaluation

    def evaluate(self):
        """
        Evaluate the label classifier on a given DataFrame.

        Evaluates by computing the accuracy, balanced accuracy and f1-score across all labels
        plus the f1-score, precision and recall across each label individually.
        """
        # copy the df as we do not want to modify it
        df = self.df_test.copy()

        # get probability of each class
        _results = pandas.DataFrame(
            data=self.clf.predict_proba(X=df[self.label_feature_list]), columns=self.clf.classes_
        )

        # get predicted label index over all classes
        predicted_label_list = list(_results.idxmax(axis=1))
        # get predicted label probability over all classes
        accuracy_list = list(_results.max(axis=1))

        # get another dataframe with only the probability over the classes that aren't NO_LABEL
        _results_only_label = pandas.DataFrame()
        if 'NO_LABEL' in _results.columns:
            _results_only_label = _results.drop(['NO_LABEL'], axis=1)

        if _results_only_label.shape[1] > 0:
            # get predicted label index over all classes that are not NO_LABEL
            only_label_predicted_label_list = list(_results_only_label.idxmax(axis=1))
            # get predicted label probability over all classes that are not NO_LABEL
            only_label_accuracy_list = list(_results_only_label.max(axis=1))

            # for each predicted label (over all classes)
            for index in range(len(predicted_label_list)):
                # if the highest probability to a non NO_LABEL class is >=0.2, we say it predicted that class instead
                # replace predicted label index and probability
                if only_label_accuracy_list[index] >= 0.2:  # todo: why 0.2
                    predicted_label_list[index] = only_label_predicted_label_list[index]
                    accuracy_list[index] = only_label_accuracy_list[index]
        else:
            logger.info('\n[WARNING] _results_only_label is empty.\n')

        # add a column for predicted label index
        df.insert(loc=0, column='predicted_label_text', value=predicted_label_list)

        # add a column for prediction probability (not actually accuracy)
        df.insert(loc=0, column='Accuracy', value=accuracy_list)

        # get and sort the importance of each feature
        feature_importances = self.clf.feature_importances_

        feature_importances_list = sorted(
            list(zip(self.label_feature_list, feature_importances)), key=lambda item: item[1], reverse=True
        )

        # computes the general metrics, i.e. across all labels
        y_true = df['label_text']
        y_pred = df['predicted_label_text']

        # gets accuracy, balanced accuracy and f1-score over all labels
        results_general = {
            'label': 'general/all annotations',
            'accuracy': accuracy_score(y_true, y_pred),
            'balanced accuracy': balanced_accuracy_score(y_true, y_pred),
            'f1-score': f1_score(y_true, y_pred, average='weighted'),
        }

        # gets accuracy, balanced accuracy and f1-score over all labels (except for 'NO_LABEL'/'NO_LABEL')
        y_true_filtered = []
        y_pred_filtered = []
        for s_true, s_pred in zip(y_true, y_pred):
            if not (s_true == 'NO_LABEL' and s_pred == 'NO_LABEL'):
                y_true_filtered.append(s_true)
                y_pred_filtered.append(s_pred)
        results_general_filtered = {
            'label': 'all annotations except TP of NO_LABEL',
            'accuracy': accuracy_score(y_true_filtered, y_pred_filtered),
            'balanced accuracy': balanced_accuracy_score(y_true_filtered, y_pred_filtered),
            'f1-score': f1_score(y_true_filtered, y_pred_filtered, average='weighted'),
        }

        # compute all metrics again, but per label
        labels = list(set(df['label_text']))
        precision, recall, fscore, support = precision_recall_fscore_support(y_pred, y_true, labels=labels)

        # store results for each label
        results_labels_list = []

        for i, label in enumerate(labels):
            results = {
                'label': label,
                'accuracy': None,
                'balanced accuracy': None,
                'f1-score': fscore[i],
                'precision': precision[i],
                'recall': recall[i],
            }
            results_labels_list.append(results)

        # sort results for each label in descending order by their f1-score
        results_labels_list_sorted = sorted(results_labels_list, key=lambda k: k['f1-score'], reverse=True)

        # combine general results and label specific results into one dict
        results_summary = {
            'general': results_general,
            'general_filtered': results_general_filtered,
            'label-specific': results_labels_list_sorted,
        }

        # get the probability_distribution
        prob_dict = self._get_probability_distribution(df, start_from=0.2)
        prob_list = [(k, v) for k, v in prob_dict.items()]
        prob_list.sort(key=lambda tup: tup[0])
        df_prob = pandas.DataFrame(prob_list, columns=['Range of predicted Accuracy', 'Real Accuracy in this range'])

        # log results and feature importance and probability distribution as tables
        logger.info(
            '\n'
            + tabulate(
                pandas.DataFrame([results_general, results_general_filtered] + results_labels_list_sorted),
                floatfmt=".1%",
                headers="keys",
                tablefmt="pipe",
            )
            + '\n'
        )

        logger.info(
            '\n'
            + tabulate(
                pandas.DataFrame(feature_importances_list, columns=['feature_name', 'feature_importance']),
                floatfmt=".4%",
                headers="keys",
                tablefmt="pipe",
            )
            + '\n'
        )

        logger.info('\n' + tabulate(df_prob, floatfmt=".2%", headers="keys", tablefmt="pipe") + '\n')

        return results_summary

    def _get_probability_distribution(self, df, start_from=0.2):
        """Calculate the probability distribution according to the range of confidence."""
        # group by accuracy
        step_size = 0.1
        step_list = numpy.arange(start_from, 1 + step_size, step_size)
        df_dict = {}
        for index, step in enumerate(step_list):
            if index + 1 < len(step_list):
                lower_bound = round(step, 2)
                upper_bound = round(step_list[index + 1], 2)
                df_range = df[df['Accuracy'].between(lower_bound, upper_bound)]
                df_range_acc = accuracy_score(df_range['label_text'], df_range['predicted_label_text'])
                df_dict[str(lower_bound) + '-' + str(upper_bound)] = df_range_acc

        return df_dict

    # def _filter_annotations_for_duplicates(self, doc_annotations_list: List['Annotation']):
    #     """
    #     Filter the annotations for duplicates.
    #
    #     A duplicate is characterized by having the same start_offset,
    #     end_offset and label_text. Duplicates have to be filtered as there should be only one logical truth per
    #     specific text_offset and label.
    #     """
    #     annotations_filtered = []
    #     res = collections.defaultdict(list)
    #
    #     for annotation in doc_annotations_list:
    #         key = f'{annotation.start_offset}"_"{annotation.end_offset}'
    #         res[key].append(annotation)
    #
    #     annotations_bundled = list(res.values())
    #     for annotation_cluster in annotations_bundled:
    #         if len(annotation_cluster) > 1:
    #             found = False
    #             for annotation in annotation_cluster:
    #                 if annotation.is_correct is True:
    #                     found = True
    #                     annotations_filtered.append(annotation)
    #
    #             if found is False:
    #                 annotations_filtered.append(annotation_cluster[0])
    #
    #         else:
    #             annotations_filtered.append(annotation_cluster[0])
    #
    #     return annotations_filtered


class SeparateLabelsAnnotationMultiClassModel(DocumentAnnotationMultiClassModel):
    """
    Model that should be used when we want to treat labels shared by different templates as different labels.

    The extract method needs to undo the changes done in the labels of the project (project.separate_labels()).
    """

    def __init__(self, *args, **kwargs):
        """Initialize DocumentEntityMulticlassModel."""
        DocumentAnnotationMultiClassModel.__init__(self, *args, **kwargs)

    def extract(self, document: Document) -> 'Dict':
        """
        Undo the renaming of the labels when using project.separate_labels().

        In this way we have the output of the extraction in the correct format.
        """
        res_dict = DocumentAnnotationMultiClassModel.extract(self, document=document)

        new_res = {}
        for key, value in res_dict.items():
            # if the value is a list, is because the key corresponds to a section label with multiple sections
            # the key has already the name of the section label
            # we need to go to each element of the list, which is a dictionary, and
            # rewrite the label name (remove the section label name) in the keys
            if isinstance(value, list):
                section_label = key
                if section_label not in new_res.keys():
                    new_res[section_label] = []

                for found_section in value:
                    new_found_section = {}
                    for label, df in found_section.items():
                        if '__' in label:
                            label = label.split('__')[1]
                            df.label_text = label
                            df.label = label
                        new_found_section[label] = df

                    new_res[section_label].append(new_found_section)

            # if the value is a dictionary, is because he key corresponds to a section label without multiple sections
            # we need to rewrite the label name (remove the section label name) in the keys
            elif isinstance(value, dict):
                section_label = key
                if section_label not in new_res.keys():
                    new_res[section_label] = {}

                for label, df in value.items():
                    if '__' in label:
                        label = label.split('__')[1]
                        df.label_text = label
                        df.label = label
                    new_res[section_label][label] = df

            # otherwise the value must be directly a dataframe and it will correspond to the default section
            # can also correspond to labels which the template clf couldn't attribute to any template.
            # so we still check if we have the changed label name
            elif '__' in key:
                section_label = key.split('__')[0]
                if section_label not in new_res.keys():
                    new_res[section_label] = {}
                key = key.split('__')[1]
                value.label_text = key
                value.label = key
                # if the section label already exists and allows multi sections
                if isinstance(new_res[section_label], list):
                    new_res[section_label].append({key: value})
                else:
                    new_res[section_label][key] = value
            else:
                new_res[key] = value

        return new_res


class DocumentEntityMulticlassModel(DocumentAnnotationMultiClassModel, GroupAnnotationSets):
    """Creates Annotations by extracting all entities and then finding which overlap with existing annotations."""

    def __init__(self, *args, **kwargs):
        """Initialize DocumentEntityMulticlassModel."""
        DocumentAnnotationMultiClassModel.__init__(self, *args, **kwargs)

        self.use_generic_regex = False
        self.multiline_labels = []

        # set tokenizer if it wasn't set already
        if not hasattr(self, "tokenizer"):
            self.tokenizer = WhitespaceTokenizer()

    # def get_annotations(self):
    #     """Convert Documents to entities."""
    #     logger.info('Getting annotations')
    #
    #     # loop over each document and test document
    #     for document in self.documents + self.test_documents:
    #         document = self.tokenizer.tokenize(document)
    #
    #         # flush existing annotations
    #         annotations = document._annotations
    #         document._annotations = []
    #
    #         # check for multiline annotations
    #         annotations = split_multiline_annotations(annotations, self.multiline_labels)
    #
    #         # flush annotations added during split of multiline
    #         document._annotations = []
    #
    #         # get exact matches
    #         matches = document.annotations()
    #         remaining_annotations = list(set(document.annotations(use_correct=False))-set(document.annotations()))
    # matches, remaining_annotations, remaining_entities = self.get_exact_matches_and_filter_mached(
    #     annotations, document.annotations()
    # )

    # get entity matches for the rest
    # (only call after the first one as this filters out annotations with no start and/or end offset)
    # matches += self.get_entity_matches(remaining_annotations, remaining_entities)

    # convert the matches to annotations and add them to the document
    # self.add_matches_as_document_annotation(matches, document)

    # logger.info('All annotations processed.')

    # def add_matches_as_document_annotation(self, matches, document):
    #     """Convert a match into a fitting annotation."""
    #     # get document bbox for creating annotations
    #     bbox = document.get_bbox()
    #
    #     # go through the matches
    #     for match in matches:
    #         # create annotation from entity
    #         e = Annotation(
    #             start_offset=match['entity']['start_offset'],
    #             end_offset=match['entity']['end_offset'],
    #             document=document,
    #             bbox=get_bbox(
    #                 bbox, start_offset=match['entity']['start_offset'], end_offset=match['entity']['end_offset']
    #             ),
    #         )
    #
    #         # set the corresponding attributes for the entity annotation to match that of the actual annotation
    #         for key, value in match['annotation'].__dict__.items():
    #             if key in ['start_offset', 'end_offset', 'offset_string', 'offset_string_original']:
    #                 continue
    #             setattr(e, key, value)
    #
    #         # add the entity as an annotation to the document
    #         document.add_annotation(e)

    # def get_exact_matches_and_filter_mached(self, annotations, entities) -> Tuple[List[Dict], List[Dict]]:
    #     """
    #     Only give back annotation-entity combinations that are considered exact.
    #
    #     This is the case if annotation spans the entire entity (or more).
    #     If an annotation is not completely exactly matched, add it to the unmatched list
    #     """
    #     unmachted_list = []
    #     match_list = []
    #     matched_entities = []
    #
    #     for annotation in annotations:
    #
    #         # filter out annotations with missing start or end offset
    #         if annotation.start_offset is None or annotation.end_offset is None:
    #             continue
    #
    #         start_found = False
    #         end_found = False
    #         for entity in entities:
    #             # check if the annotation spans the whole entity
    #             if entity['start_offset'] >= annotation.
    #             start_offset and entity['end_offset'] <= annotation.end_offset:
    #                 match_list.append({'entity': entity, 'annotation': annotation})
    #
    #                 # you can't just remove the entity because that causes the iterator to jump by one
    #                 matched_entities.append(entity)
    #
    #                 # update start and end found
    #                 if entity['start_offset'] == annotation.start_offset:
    #                     start_found = True
    #                 if entity['end_offset'] == annotation.end_offset:
    #                     end_found = True
    #
    #             # if we go past the annotation end, stop looking for entities (assuming they are in order)
    #             elif entity['start_offset'] > annotation.end_offset:
    #                 break
    #
    #         # see if the annotation was completly machted
    #         if not (start_found and end_found):
    #             unmachted_list.append(annotation)
    #
    #     return match_list, unmachted_list, [entity for entity in entities if entity not in matched_entities]

    # def get_entity_matches(self, annotations, entities) -> List[Dict]:
    #     """Catch all exceptetions. Here we just match every entity with an annotation that lies within them."""
    #     matches = []
    #
    #     for annotation in annotations:
    #         for entity in entities:
    #             # matches if entity starts before the annotation ends and ends after the annotation starts
    #             if entity['start_offset'] <= annotation.
    #             end_offset and entity['end_offset'] >= annotation.start_offset:
    #                 matches.append({'entity': entity, 'annotation': annotation})
    #             # if we go past the annotation end, stop looking for entities (assuming they are in order)
    #             elif entity['start_offset'] > annotation.end_offset:
    #                 break
    #
    #     return matches

    def extract(self, document: Document) -> Dict:
        """Run clf."""
        res_dict = super().extract(document=document)

        label_type_dict = {label.name: label.data_type for label in self.category.labels}
        label_threshold_dict = {
            label.name: label.threshold if hasattr(label, 'threshold') else 0.1 for label in self.category.labels
        }

        res_dict = remove_empty_dataframes_from_extraction(res_dict)
        res_dict = filter_low_confidence_extractions(res_dict, label_threshold_dict)

        merged_res_dict = merge_annotations(
            res_dict=res_dict,
            doc_text=document.text,
            label_type_dict=label_type_dict,
            doc_bbox=document.get_bbox(),
            labels_threshold=label_threshold_dict,
        )

        # If the training has labels with multiline annotations, we try to merge entities vertically
        if hasattr(self, 'multiline_labels'):
            multiline_labels_names = [label.name for label in self.multiline_labels]
            merged_res_dict = merge_annotations(
                res_dict=merged_res_dict,
                doc_text=document.text,
                label_type_dict=label_type_dict,
                doc_bbox=document.get_bbox(),
                multiline_labels_names=multiline_labels_names,
                merge_vertical=True,
                labels_threshold=label_threshold_dict,
            )

        return merged_res_dict


class SeparateLabelsEntityMultiClassModel(DocumentEntityMulticlassModel):
    """
    Model that should be used when we want to treat labels shared by different templates as different labels.

    The extract method needs to undo the changes done in the labels of the project (project.separate_labels()).
    """

    def __init__(self, extract_threshold=None, *args, **kwargs):
        """Initialize DocumentEntityMulticlassModel."""
        DocumentEntityMulticlassModel.__init__(self, *args, **kwargs)
        self.extract_threshold = extract_threshold

    def extract(self, document: Document) -> 'Dict':
        """
        Undo the renaming of the labels when using project.separate_labels().

        In this way we have the output of the extraction in the correct format.
        """
        # from konfuzio.models_labels_multiclass import DocumentEntityMulticlassModel

        res_dict = DocumentEntityMulticlassModel.extract(self, document=document)

        new_res = {}
        for key, value in res_dict.items():
            # if the value is a list, is because the key corresponds to a section label with multiple sections
            # the key has already the name of the section label
            # we need to go to each element of the list, which is a dictionary, and
            # rewrite the label name (remove the section label name) in the keys
            if isinstance(value, list):
                section_label = key
                if section_label not in new_res.keys():
                    new_res[section_label] = []

                for found_section in value:
                    new_found_section = {}
                    for label, df in found_section.items():
                        if '__' in label:
                            label = label.split('__')[1]
                            df.label_text = label
                            df.label = label
                        new_found_section[label] = df

                    new_res[section_label].append(new_found_section)

            # if the value is a dictionary, is because he key corresponds to a section label without multiple sections
            # we need to rewrite the label name (remove the section label name) in the keys
            elif isinstance(value, dict):
                section_label = key
                if section_label not in new_res.keys():
                    new_res[section_label] = {}

                for label, df in value.items():
                    if '__' in label:
                        label = label.split('__')[1]
                        df.label_text = label
                        df.label = label
                    new_res[section_label][label] = df

            # otherwise the value must be directly a dataframe and it will correspond to the default section
            # can also correspond to labels which the template clf couldn't attribute to any template.
            # so we still check if we have the changed label name
            elif '__' in key:
                section_label = key.split('__')[0]
                if section_label not in new_res.keys():
                    new_res[section_label] = {}
                key = key.split('__')[1]
                value.label_text = key
                value.label = key
                # if the section label already exists and allows multi sections
                if isinstance(new_res[section_label], list):
                    new_res[section_label].append({key: value})
                else:
                    new_res[section_label][key] = value
            else:
                new_res[key] = value

        return new_res
