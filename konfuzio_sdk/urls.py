"""URLs to the endpoints of Konfuzio Host."""

import logging

from konfuzio_sdk import KONFUZIO_HOST
from typing import Union

logger = logging.getLogger(__name__)


# TOKEN-AUTH


def get_auth_token_url(host: str = KONFUZIO_HOST) -> str:
    """
    Generate URL that creates an authentication token for the user.

    :param host: Konfuzio host
    :return: URL to generate the token.
    """
    return f"{host}/api/token-auth/"


# PROJECTS


def get_projects_list_url(host: str = KONFUZIO_HOST) -> str:
    """
    Generate URL to list all the projects available for the user.

    :param host: Konfuzio host
    :return: URL to get all the projects for the user.
    """
    return f"{host}/api/projects/"


def get_project_url(project_id: Union[int, None], host: str = KONFUZIO_HOST) -> str:
    """
    Generate URL to access the project details.

    :param host: Konfuzio host
    :param project_id: ID of the project
    :return: URL to access the project details.
    """
    return f'{host}/api/projects/{project_id}/'


def get_documents_meta_url(project_id: int, host: str = KONFUZIO_HOST) -> str:
    """
    Generate URL to load meta information about the Documents in the project.

    :param project_id: ID of the project
    :param host: Konfuzio host
    :return: URL to get all the Documents details.
    """
    return f"{host}/api/projects/{project_id}/docs/"


def get_document_segmentation_details_url(
    document_id: int, project_id: int, host: str = KONFUZIO_HOST, action='segmentation'
) -> str:
    """
    Generate URL to get the segmentation results of a  Document.

    :param document_id: ID of the Document as integer
    :param project_id: ID of the project
    :param host: Konfuzio host
    :param action: Action from where to get the results
    :return: URL to access the segmentation results of a  Document
    """
    return f'{host}/api/projects/{project_id}/docs/{document_id}/{action}/'


# DOCUMENTS


def get_upload_document_url(host: str = KONFUZIO_HOST) -> str:
    """
    Generate URL to upload a  Document.

    :param host: Konfuzio host
    :return: URL to upload a  Document
    """
    return f"{host}/api/v2/docs/"


def get_document_url(document_id: int, host: str = KONFUZIO_HOST) -> str:
    """
    Generate URL to access a  Document.

    :param document_id: ID of the Document as integer
    :param host: Konfuzio host
    :return: URL to access a  Document
    """
    return f"{host}/api/v2/docs/{document_id}/"


def get_document_ocr_file_url(document_id: int, host: str = KONFUZIO_HOST) -> str:
    """
    Generate URL to get the OCR version of the document.

    :param document_id: ID of the Document as integer
    :param host: Konfuzio host
    :return: URL to get OCR Document file.
    """
    return f'{host}/doc/show/{document_id}/'


def get_document_original_file_url(document_id: int, host: str = KONFUZIO_HOST) -> str:
    """
    Generate URL to get the original version of the document.

    :param document_id: ID of the Document as integer
    :param host: Konfuzio host
    :return: URL to get the original document
    """
    return f'{host}/doc/show-original/{document_id}/'


def get_document_api_details_url(
    document_id: int, project_id: int, host: str = KONFUZIO_HOST, extra_fields='hocr,bbox'
) -> str:
    """
    Generate URL to access the details of a Document in a project.

    :param document_id: ID of the Document as integer
    :param project_id: ID of the project
    :param host: Konfuzio host
    :param extra_fields: Extra information to include in the response
    :return: URL to get Document details
    """
    return f'{host}/api/projects/{project_id}/docs/{document_id}/?extra_fields={extra_fields}'


# LABELS


def get_labels_url(host: str = KONFUZIO_HOST) -> str:
    """
    Generate URL to list all Labels for the user.

    :param host: Konfuzio host
    :return: URL to list all Labels for the user.
    """
    return f"{host}/api/v2/labels/"


def get_label_url(label_id: int, host: str = KONFUZIO_HOST) -> str:
    """
    Generate URL to access a Label.

    :param Label_id: ID of the Label as integer
    :param host: Konfuzio host
    :return: URL to access a Label
    """
    return f"{host}/api/v2/labels/{label_id}/"


# ANNOTATIONS


def get_document_annotations_url(document_id: int, project_id: int, host: str = KONFUZIO_HOST) -> str:
    """
    Access Annotations of a document.

    :param document_id: ID of the Document as integer
    :param project_id: ID of the project
    :param host: Konfuzio host
    :return: URL to access the Annotations of a document
    """
    return f'{host}/api/projects/{project_id}/docs/{document_id}/annotations/'


def get_annotation_url(document_id: int, annotation_id: int, project_id: int, host: str = KONFUZIO_HOST) -> str:
    """
    Generate URL to access an annotation.

    :param document_id: ID of the Document as integer
    :param annotation_id: ID of the Annotation as integer
    :param project_id: ID of the project
    :param host: Konfuzio host
    :return: URL to access an Annotation of a document
    """
    return f'{host}/api/projects/{project_id}/docs/{document_id}/annotations/{annotation_id}/'


def get_create_ai_model_url(host: str = KONFUZIO_HOST) -> str:
    """
    Get url to create new AiModel.

    :return: URL
    """
    return f'{host}/api/aimodels/'


def get_update_ai_model_url(ai_model_id, host: str = KONFUZIO_HOST) -> str:
    """
    Get url to update an AiModel.

    :return: URL
    """
    return f'{host}/api/aimodels/{ai_model_id}/'
