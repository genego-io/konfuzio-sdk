"""Validate data functions."""
import logging
import os
import unittest
from copy import copy, deepcopy

import pytest
from PIL.PngImagePlugin import PngImageFile

from konfuzio_sdk.data import (
    Project,
    Annotation,
    Document,
    Label,
    AnnotationSet,
    LabelSet,
    Data,
    Span,
    download_training_and_test_data,
    Category,
    Page,
    Bbox,
)
from konfuzio_sdk.utils import is_file
from tests.variables import (
    OFFLINE_PROJECT,
    TEST_DOCUMENT_ID,
    TEST_PROJECT_ID,
    TEST_PAYSLIPS_CATEGORY_ID,
    TEST_RECEIPTS_CATEGORY_ID,
)

from konfuzio_sdk.samples import LocalTextProject

logger = logging.getLogger(__name__)


class TestOnlineProject(unittest.TestCase):
    """Use this class only to test data.py operations that need an online project."""

    annotations_correct = 24

    @classmethod
    def setUpClass(cls) -> None:
        """Initialize the test Project."""
        cls.project = Project(id_=TEST_PROJECT_ID)

    def test_document(self):
        """Test properties of a specific Documents in the test Project."""
        doc = self.project.get_document_by_id(TEST_DOCUMENT_ID)
        assert doc.category.name == 'Lohnabrechnung'
        label = self.project.labels[0]
        annotations = label.annotations(categories=[self.project.get_category_by_id(63)])
        assert len(annotations) == self.annotations_correct
        doc.update()
        annotations = label.annotations(categories=[self.project.get_category_by_id(63)])
        self.assertEqual(len(annotations), self.annotations_correct)
        assert len(doc.text) == 4537
        assert is_file(doc.txt_file_path)
        # assert is_file(doc.bbox_file_path) bbox is not loaded at this point.
        assert is_file(doc.annotation_file_path)
        assert is_file(doc.annotation_set_file_path)

    def test_document_no_label_annotations_after_update(self):
        """Test that Annotations in the no_label_annotation_set of the Document are removed after update."""
        document = self.project.get_document_by_id(TEST_DOCUMENT_ID)
        span = Span(start_offset=0, end_offset=1)
        _ = Annotation(
            document=document,
            # annotation_set=document.no_label_annotation_set,
            label=self.project.no_label,
            label_set=self.project.no_label_set,
            spans=[span],
        )
        assert len(document.annotations(use_correct=False, label=self.project.no_label)) == 1
        document.update()
        assert len(document.annotations(use_correct=False, label=self.project.no_label)) == 0

    def test_document_with_multiline_annotation(self):
        """Test properties of a specific Documents in the test Project."""
        doc = self.project.get_document_by_id(TEST_DOCUMENT_ID)
        label = self.project.get_label_by_id(867)
        annotations = label.annotations(categories=[self.project.get_category_by_id(63)])
        self.assertEqual(len(annotations), self.annotations_correct)
        doc.update()
        annotations = label.annotations(categories=[self.project.get_category_by_id(63)])
        self.assertEqual(len(annotations), self.annotations_correct)
        self.assertEqual(len(doc.text), 4537)
        # self.assertEqual(len(glob.glob(os.path.join(doc.document_folder, '*.*'))), 4)

        # existing annotation
        # https://app.konfuzio.com/admin/server/sequenceannotation/?document_id=44823&project=46
        # we are no longer filtering out the rejected annotations so it's 21
        self.assertEqual(21, len(doc.annotations(use_correct=False)))
        # a multiline Annotation in the top right corner, see https://app.konfuzio.com/a/4419937
        self.assertEqual(66, doc.annotations()[0]._spans[0].start_offset)
        self.assertEqual(78, doc.annotations()[0]._spans[0].end_offset)
        self.assertEqual(159, doc.annotations()[0]._spans[1].start_offset)
        self.assertEqual(169, doc.annotations()[0]._spans[1].end_offset)
        self.assertEqual(len(doc.annotations()), 19)
        # helm: 21.06.2022 changed from 21 to 19 as someone added (?) two annotations?
        # todo check this number, the offline project was still working fine for all evaluation tests
        self.assertTrue(doc.annotations()[0].is_online)
        with self.assertRaises(ValueError) as context:
            doc.annotations()[0].save()
            assert 'cannot update Annotations once saved online' in context.exception

    def test_get_pages_files(self):
        """Test to download page files."""
        doc = self.project.get_document_by_id(TEST_DOCUMENT_ID)
        assert len(doc.pages()) == 1

    def test_load_image_in_memory(self):
        """Test to download page files."""
        doc = self.project.get_document_by_id(TEST_DOCUMENT_ID)
        for page in doc.pages():
            image = page.get_image()
            assert type(image) is PngImageFile

    def test_get_annotation_by_id(self):
        """Test to find an online annotation by its ID."""
        doc = self.project.get_document_by_id(TEST_DOCUMENT_ID)
        annotation = doc.get_annotation_by_id(4420057)
        assert annotation.start_offset == 1507
        assert annotation.end_offset == 1518
        assert annotation.offset_string == ['Erna-Muster']

    def test_get_nonexistent_annotation_by_id(self):
        """Test to find an online annotation that does not exist by its ID, should raise an IndexError."""
        doc = self.project.get_document_by_id(TEST_DOCUMENT_ID)
        with pytest.raises(IndexError, match="is not part of"):
            _ = doc.get_annotation_by_id(999999)

    def test_create_annotation_offline(self):
        """Test to add an Annotation to the document offline, and that it does not persist after updating the doc."""
        doc = self.project.get_document_by_id(TEST_DOCUMENT_ID)
        assert Span(start_offset=1590, end_offset=1602) not in doc.spans()
        label = self.project.get_label_by_name('Lohnart')
        annotation = Annotation(
            document=doc,
            spans=[Span(start_offset=1590, end_offset=1602)],
            label=label,
            label_set=label.label_sets[0],
            accuracy=1.0,
            is_correct=True,
        )
        assert annotation in doc.annotations()
        doc.update()  # redownload document information to check that the annotation was not added online
        assert annotation not in doc.annotations()

    def test_create_annotation_then_delete_annotation(self):
        """Test to add an Annotation to the document online, then to delete it offline and online as well."""
        # We do 3 tests in 1 here since unit tests should be independent,
        # we don't want to refer to an Annotation created by a previous test

        # Test1: add an Annotation to the document online
        doc = self.project.get_document_by_id(TEST_DOCUMENT_ID)
        assert Span(start_offset=1590, end_offset=1602) not in doc.spans()
        label = self.project.get_label_by_name('Lohnart')
        annotation = Annotation(
            document=doc,
            spans=[Span(start_offset=1590, end_offset=1602)],
            label=label,
            label_set=label.label_sets[0],
            accuracy=1.0,
            is_correct=True,
        )
        annotation.save()
        assert annotation in doc.annotations()
        doc.update()  # redownload document information to check that the annotation was saved online
        assert annotation in doc.annotations()

        # Test2: delete the Annotation from the document offline
        annotation.delete(delete_online=False)
        assert annotation not in doc.get_annotations()
        doc.update()  # redownload document information to check that the annotation was not deleted online
        assert annotation in doc.get_annotations()

        # Test3: delete the Annotation from the document online.
        annotation.delete()  # doc.update() performed internally when delete_online=True, which is default
        assert annotation not in doc.get_annotations()


class TestOfflineExampleData(unittest.TestCase):
    """Test data features without real data."""

    @classmethod
    def setUpClass(cls) -> None:
        """Initialize the test Project."""
        cls.project = Project(id_=None, project_folder=OFFLINE_PROJECT)
        cls.payslips_category = cls.project.get_category_by_id(TEST_PAYSLIPS_CATEGORY_ID)
        cls.receipts_category = cls.project.get_category_by_id(TEST_RECEIPTS_CATEGORY_ID)

    @classmethod
    def tearDownClass(cls) -> None:
        """Control the number of Documents created in the Test."""
        assert len(cls.payslips_category.documents()) == 25
        assert len(cls.receipts_category.documents()) == 25
        assert cls.project.get_document_by_id(44864).category is None
        assert len(cls.project.documents) == 51

    def test_copy(self):
        """Test that copy is not allowed as it needs to be implemented for every SDK concept."""
        data = Data()
        with pytest.raises(NotImplementedError):
            copy(data)

    def test_deepcopy(self):
        """Test that deeepcopy is not allowed as it needs to be implemented for every SDK concept."""
        data = Data()
        with pytest.raises(NotImplementedError):
            deepcopy(data)

    def test_document_copy(self) -> None:
        """Test to create a new Document instance."""
        document = self.project.get_document_by_id(TEST_DOCUMENT_ID)
        new_document = deepcopy(document)
        assert new_document != document
        assert new_document.get_page_by_index(0).width == 595.2
        assert new_document._annotations is None  # for now the implementation just copies the bbox and text

    def test_project_num_label(self):
        """Test that no_label exists in the Labels of the Project and has the expected name."""
        self.assertEqual(19, len(self.payslips_category.labels))
        self.assertEqual(30, len(self.receipts_category.labels))
        self.assertEqual(19 + 30 - 1, len(self.project.labels))  # subtract one to avoid double counting the NO_LABEL

    def test_no_label(self):
        """Test if NO_LABEL is available."""
        assert self.project.no_label.name == "NO_LABEL"
        self.assertIn(self.project.no_label, self.project.labels)

    def test_annotation_bbox(self):
        """Create a Span and calculate it's bbox."""
        span = Span(start_offset=1764, end_offset=1769)  # the correct Annotation spans 1763 to 1769
        document = self.project.get_document_by_id(TEST_DOCUMENT_ID)
        _ = Annotation(
            id_=None,
            document=document,
            is_correct=True,
            annotation_set=document.annotation_sets()[0],
            label=self.project.no_label,
            label_set=self.project.label_sets[0],
            spans=[span],
        )
        box = span.bbox()  # verify if we can calculate valid bounding boxes from a given Text offset.
        assert box.x1 == 113.28
        assert box.x0 == 84.28
        assert box.y0 == 532.592
        assert box.y1 == 540.592

    def test_get_category_name_for_fallback_prediction(self):
        """Test turn a category name to lowercase, remove parentheses along with their contents, and trim spaces."""
        assert self.payslips_category.fallback_name == "lohnabrechnung"
        assert self.receipts_category.fallback_name == "quittung"
        test_category = Category(project=self.project, id_=1, name="Te(s)t Category Name (content content)")
        assert test_category.fallback_name == "tet category name"


class TestEqualityAnnotation(unittest.TestCase):
    """Test the equality of Annotations."""

    @classmethod
    def setUpClass(cls) -> None:
        """Initialize the test Project."""
        cls.project = Project(id_=None)
        cls.label_one = Label(project=cls.project, text='First')
        cls.label_two = Label(project=cls.project, text='First')
        cls.category = Category(project=cls.project, id_=1)
        cls.document = Document(project=cls.project, category=cls.category)
        cls.label_set = LabelSet(project=cls.project, categories=[cls.category], id_=421)
        # cls.label_set.add_label(cls.label)
        cls.annotation_set = AnnotationSet(document=cls.document, label_set=cls.label_set)
        assert len(cls.project.virtual_documents) == 1

    def test_overlapping_correct_same_label(self):
        """Reject to add Annotations that are identical."""
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        second_span = Span(start_offset=1, end_offset=2)
        _ = Annotation(
            document=document, spans=[first_span], label_set=self.label_set, label=self.label_one, is_correct=True
        )

        with pytest.raises(ValueError) as e:
            _ = Annotation(
                document=document, spans=[second_span], label_set=self.label_set, label=self.label_one, is_correct=True
            )
            assert 'is a duplicate of' in str(e)

    def test_partially_overlapping_correct_same_label(self):
        """Accept to add Annotation with the same Label if parts of their Spans differ."""
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        second_span = Span(start_offset=1, end_offset=2)
        third_span = Span(start_offset=2, end_offset=3)
        _ = Annotation(
            document=document, spans=[first_span], label_set=self.label_set, label=self.label_one, is_correct=True
        )

        _ = Annotation(
            document=document,
            spans=[second_span, third_span],
            label_set=self.label_set,
            label=self.label_one,
            is_correct=True,
        )

    def test_overlapping_wrong_same_label(self):
        """Accept to add Annotation with the same Label if both are not correct."""
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        second_span = Span(start_offset=1, end_offset=2)
        _ = Annotation(
            document=document, spans=[first_span], label_set=self.label_set, label=self.label_one, is_correct=False
        )

        with pytest.raises(ValueError) as e:
            _ = Annotation(
                document=document, spans=[second_span], label_set=self.label_set, label=self.label_one, is_correct=False
            )
            assert 'is a duplicate of' in str(e)

    def test_partially_overlapping_wrong_same_label(self):
        """Accept to add Annotation with the same Label if parts of their Spans differ and one is not correct."""
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        second_span = Span(start_offset=1, end_offset=2)
        third_span = Span(start_offset=2, end_offset=3)
        _ = Annotation(
            document=document, spans=[first_span], label_set=self.label_set, label=self.label_one, is_correct=False
        )

        _ = Annotation(
            document=document,
            spans=[second_span, third_span],
            label_set=self.label_set,
            label=self.label_one,
            is_correct=False,
        )

    def test_overlapping_partially_correct_same_label(self):
        """Accept to add Annotation with the same Label if one Annotation is not correct."""
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        second_span = Span(start_offset=1, end_offset=2)
        _ = Annotation(
            document=document, spans=[first_span], label_set=self.label_set, label=self.label_one, is_correct=True
        )

        with pytest.raises(ValueError) as e:
            _ = Annotation(
                document=document, spans=[second_span], label_set=self.label_set, label=self.label_one, is_correct=False
            )
            assert 'is a duplicate of' in str(e)

    def test_partially_overlapping_partially_correct_same_label(self):
        """Accept to add Annotation with the same Label if parts of their Spans differ and one is not correct."""
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        second_span = Span(start_offset=1, end_offset=2)
        third_span = Span(start_offset=2, end_offset=3)
        _ = Annotation(
            document=document, spans=[first_span], label_set=self.label_set, label=self.label_one, is_correct=True
        )

        _ = Annotation(
            document=document,
            spans=[second_span, third_span],
            label_set=self.label_set,
            label=self.label_one,
            is_correct=False,
        )

    def test_overlapping_correct_other_label(self):
        """Accept to add Annotation with different Labels."""
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        second_span = Span(start_offset=1, end_offset=2)
        _ = Annotation(
            document=document, spans=[first_span], label_set=self.label_set, label=self.label_two, is_correct=True
        )

        _ = Annotation(
            document=document, spans=[second_span], label_set=self.label_set, label=self.label_one, is_correct=True
        )

    def test_overlapping_wrong_other_label(self):
        """Accept to add Annotation with different Labels if both are not correct."""
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        second_span = Span(start_offset=1, end_offset=2)
        _ = Annotation(
            document=document, spans=[first_span], label_set=self.label_set, label=self.label_one, is_correct=False
        )

        _ = Annotation(
            document=document, spans=[second_span], label_set=self.label_set, label=self.label_two, is_correct=False
        )

    def test_partially_overlapping_partially_correct_other_label(self):
        """Accept to add Annotation with different Labels if one is not correct and one is only some Spans overlap."""
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        second_span = Span(start_offset=1, end_offset=2)
        third_span = Span(start_offset=2, end_offset=3)
        _ = Annotation(
            document=document, spans=[first_span], label_set=self.label_set, label=self.label_one, is_correct=True
        )

        _ = Annotation(
            document=document,
            spans=[second_span, third_span],
            label_set=self.label_set,
            label=self.label_two,
            is_correct=False,
        )


class TestOfflineDataSetup(unittest.TestCase):
    """Test data features on programmatically constructed Project."""

    @classmethod
    def setUpClass(cls) -> None:
        """Initialize the test Project."""
        cls.project = Project(id_=None)
        cls.label = Label(project=cls.project, text='First Offline Label')
        cls.category = Category(project=cls.project, id_=1)
        cls.document = Document(project=cls.project, category=cls.category)
        cls.label_set = LabelSet(project=cls.project, categories=[cls.category], id_=421)
        cls.label_set.add_label(cls.label)
        cls.annotation_set = AnnotationSet(document=cls.document, label_set=cls.label_set)
        assert len(cls.project.virtual_documents) == 1

    @classmethod
    def tearDownClass(cls) -> None:
        """Control the number of Documents created in the Test."""
        assert len(cls.project.virtual_documents) == 47

    # def test_document_only_needs_project(self):
    #     """Test that a Document can be created without category"""
    #     _ = Document(project=self.project)

    def test_project_no_label(self):
        """Test that no_label exists in the Labels of the Project and has the expected name."""
        assert self.project.no_label in self.project.labels
        assert self.project.no_label.name == "NO_LABEL"

    def test_project_no_label_set(self):
        """Test that no_label_set exists in the Label Sets of the Project."""
        assert self.project.no_label_set in self.project.label_sets

    def test_project_has_category(self):
        """Test that no_label_set exists in the Label Sets of the Categories of the Project."""
        assert self.category in self.project.categories

    def test_project_no_label_set_in_all_categories(self):
        """Test that no_label_set exists in the Label Sets of the Categories of the Project."""
        for category in self.project.categories:
            assert self.project.no_label_set in category.project.label_sets

    def test_document_no_label_annotation_set_label_set(self):
        """Test that Label Set of the no_label_annotation_set of the Document has the no_label_set of the Project."""
        assert self.document.no_label_annotation_set.label_set == self.project.no_label_set

    def test_category_of_document(self):
        """Test if setup worked."""
        assert self.document.category == self.category

    @unittest.skip(reason='Span validation.')
    def test_span_negative_offset(self):
        """Negative Span creation should not be possible."""
        project = Project(id_=None)
        category = Category(project=project)
        label_set = LabelSet(id_=33, project=project, categories=[category])
        label = Label(id_=22, text='LabelName', project=project, label_sets=[label_set], threshold=0.5)
        document = Document(project=project, category=category, text="From 14.12.2021 to 1.1.2022.", dataset_status=2)
        with self.assertRaises():
            span_1 = Span(start_offset=-1, end_offset=2)
            annotation_set_1 = AnnotationSet(id_=1, document=document, label_set=label_set)
            _ = Annotation(
                document=document,
                is_correct=True,
                annotation_set=annotation_set_1,
                label=label,
                label_set=label_set,
                spans=[span_1],
            )

    def test_training_document_annotations_are_available(self):
        """Test if the Label can access the new Annotation."""
        project = Project(id_=None)
        category = Category(project=project)
        label_set = LabelSet(id_=33, project=project, categories=[category])
        label = Label(id_=22, text='LabelName', project=project, label_sets=[label_set], threshold=0.5)
        document = Document(project=project, category=category, text="From 14.12.2021 to 1.1.2022.", dataset_status=2)
        span_1 = Span(start_offset=5, end_offset=15)
        annotation_set_1 = AnnotationSet(id_=1, document=document, label_set=label_set)
        annotation = Annotation(
            document=document,
            is_correct=True,
            annotation_set=annotation_set_1,
            label=label,
            label_set=label_set,
            spans=[span_1],
        )
        assert label.annotations(categories=[category]) == [annotation]

    def test_add_annotation_with_label_set_none(self):
        """Test to add an Annotation to a Document where the LabelSet is None."""
        project = Project(id_=None)
        category = Category(project=project)
        label_set = LabelSet(project=project, categories=[category])
        label = Label(project=project, label_sets=[label_set])
        # create a document A
        document_a = Document(project=project, category=category)
        _ = Span(start_offset=1, end_offset=2)
        annotation_set_a = AnnotationSet(document=document_a, label_set=label_set)

        with self.assertRaises(ValueError) as context:
            Annotation(document=document_a, annotation_set=annotation_set_a, label=label)
            assert 'has no Label Set' in context.exception

    def test_to_get_threshold(self):
        """Define fallback threshold for a Label."""
        project = Project(id_=None)
        label = Label(project=project, text='Third Offline Label')
        assert label.threshold == 0.0

    def test_to_add_label_to_project(self):
        """Add one Label to a Project."""
        _ = Label(project=self.project, text='Third Offline Label')
        assert sorted([label.name for label in self.project.labels]) == [
            'First Offline Label',
            'NO_LABEL',
            'Second Offline Label',
            'Third Offline Label',
        ]

    def test_label_has_label_sets(self):
        """Pass and store Label Sets."""
        project = Project(id_=None)
        label = Label(project=project, label_sets=[self.label_set], text='Second Offline Label')
        assert [ls.id_ for ls in label.label_sets] == [421]

    def test_to_add_label_to_project_twice(self):
        """Add an existing Label to a Project."""
        with self.assertRaises(ValueError):
            self.project.add_label(self.label)

    def test_get_labels_of_category(self):
        """Return only related Labels as Information Extraction can be trained per Category."""
        assert self.category.labels.__len__() == 1

    def test_to_add_spans_to_annotation(self):
        """Add one Span to one Annotation."""
        document = Document(project=self.project, category=self.category)
        span = Span(start_offset=1, end_offset=2)
        annotation = Annotation(document=document, spans=[span], label=self.label, label_set=self.label_set)
        self.assertEqual([span], annotation.spans)

    def test_span_reference_to_annotation(self):
        """Test Span reference to Annotation."""
        document = Document(project=self.project, category=self.category)
        span = Span(start_offset=1, end_offset=2)
        annotation = Annotation(document=document, spans=[span], label=self.label, label_set=self.label_set)
        assert annotation.spans[0].annotation is not None
        assert annotation.spans[0].bbox() is None  # Span bboxes must be explicitly loaded using span.bbox
        # Here this would be failing even when calling span.bbox as the test document does not have a bbox.

    def test_get_span_bbox_with_characters_without_height_allowed(self):
        """
        Test get the bbox of a Span where the characters do not have height (OCR problem).

        Without specifying strict validation, we allow such bboxes.
        """
        document_bbox = {'1': {'text': 'e', 'x0': 0, 'x1': 1, 'y0': 1, 'y1': 1, 'page_number': 1}}
        document = Document(project=self.project, category=self.category, text='hello', bbox=document_bbox)
        span = Span(start_offset=1, end_offset=2)
        _ = Annotation(document=document, spans=[span], label=self.label, label_set=self.label_set)
        _ = Page(id_=1, number=1, original_size=(595.2, 300.0), document=document, start_offset=0, end_offset=1)
        self.assertTrue(span.bbox())

    def test_get_span_bbox_with_characters_without_height_strict_validation(self):
        """
        Test get the bbox of a Span where the characters do not have height (OCR problem).

        With strict validation specified, we don't allow such bboxes.
        """
        document_bbox = {'1': {'text': 'e', 'x0': 0, 'x1': 1, 'y0': 1, 'y1': 1, 'page_number': 1}}
        document = Document(
            project=self.project, category=self.category, text='hello', bbox=document_bbox, strict_bbox_validation=True
        )
        span = Span(start_offset=1, end_offset=2)
        _ = Annotation(document=document, spans=[span], label=self.label, label_set=self.label_set)
        _ = Page(id_=1, number=1, original_size=(595.2, 300.0), document=document, start_offset=0, end_offset=1)
        with pytest.raises(ValueError, match='has no height in Page 0.'):
            span.bbox()

    def test_get_span_bbox_with_characters_without_width_missing_bbox(self):
        """Test get the bbox of a Span where the characters do not have width (OCR problem)."""
        document_bbox = {'1': {'x0': 1, 'x1': 1, 'y0': 0, 'y1': 1, 'page_number': 1}}
        document = Document(project=self.project, category=self.category, text='hello', bbox=document_bbox)
        span = Span(start_offset=1, end_offset=2)
        _ = Annotation(document=document, spans=[span], label=self.label, label_set=self.label_set)
        _ = Page(id_=1, number=1, original_size=(595.2, 300.0), document=document, start_offset=0, end_offset=1)
        with pytest.raises(ValueError, match='provides Character "None" document text refers to "e"'):
            span.bbox()

    def test_get_span_bbox_with_characters_without_width_allowed(self):
        """
        Test get the bbox of a Span where the characters do not have width (OCR problem).

        Without strict validation specified, we allow such bboxes.
        """
        document_bbox = {'0': {'x0': 1, 'x1': 1, 'y0': 0, 'y1': 1, 'page_number': 1, 'text': 'h'}}
        document = Document(project=self.project, category=self.category, text='hello', bbox=document_bbox)
        span = Span(start_offset=0, end_offset=1)
        _ = Annotation(document=document, spans=[span], label=self.label, label_set=self.label_set)
        _ = Page(id_=1, number=1, original_size=(595.2, 300.0), document=document, start_offset=0, end_offset=1)
        self.assertTrue(span.bbox())

    def test_get_span_bbox_with_characters_without_width_strict_validation(self):
        """
        Test get the bbox of a Span where the characters do not have width (OCR problem).

        With strict validation specified, we allow such bboxes.
        """
        document_bbox = {'0': {'x0': 1, 'x1': 1, 'y0': 0, 'y1': 1, 'page_number': 1, 'text': 'h'}}
        document = Document(
            project=self.project, category=self.category, text='hello', bbox=document_bbox, strict_bbox_validation=True
        )
        span = Span(start_offset=0, end_offset=1)
        _ = Annotation(document=document, spans=[span], label=self.label, label_set=self.label_set)
        _ = Page(id_=1, number=1, original_size=(595.2, 300.0), document=document, start_offset=0, end_offset=1)
        with pytest.raises(ValueError, match='has no width in Page 0'):
            span.bbox()

    def test_get_span_bbox_with_characters_with_negative_x_coord(self):
        """Test get the bbox of a Span where the characters have negative x coordinates (OCR problem)."""
        document_bbox = {'1': {'text': 'e', 'x0': -1, 'x1': 1, 'y0': 0, 'y1': 1, 'page_number': 1}}
        document = Document(project=self.project, category=self.category, text='hello', bbox=document_bbox)
        span = Span(start_offset=1, end_offset=2)
        _ = Annotation(document=document, spans=[span], label=self.label, label_set=self.label_set)
        _ = Page(id_=1, number=1, original_size=(595.2, 300.0), document=document, start_offset=0, end_offset=3)
        with pytest.raises(ValueError, match='negative x coordinate'):
            span.bbox()

    def test_get_span_bbox_with_characters_with_negative_y_coord(self):
        """Test get the bbox of a Span where the characters have negative x coordinates (OCR problem)."""
        document_bbox = {'1': {'text': 'e', 'x0': 0, 'x1': 1, 'y0': -1, 'y1': 1, 'page_number': 1}}
        document = Document(project=self.project, category=self.category, text='hello', bbox=document_bbox)
        span = Span(start_offset=1, end_offset=2)
        _ = Annotation(document=document, spans=[span], label=self.label, label_set=self.label_set)
        _ = Page(id_=1, number=1, original_size=(595.2, 300.0), document=document, start_offset=0, end_offset=3)
        with pytest.raises(ValueError, match='negative y coordinate'):
            span.bbox()

    def test_get_span_bbox_with_characters_with_x_coord_outside_page_width(self):
        """Test get the bbox of a Span where the characters have negative x coordinates (OCR problem)."""
        document_bbox = {'1': {'text': 'e', 'x0': 596, 'x1': 597, 'y0': 0, 'y1': 1, 'page_number': 1}}
        document = Document(project=self.project, category=self.category, text='hello', bbox=document_bbox)
        span = Span(start_offset=1, end_offset=2)
        _ = Annotation(document=document, spans=[span], label=self.label, label_set=self.label_set)
        _ = Page(id_=1, number=1, original_size=(595.2, 300.0), document=document, start_offset=0, end_offset=3)
        with pytest.raises(ValueError, match='exceeds width of Page 0'):
            span.bbox()

    def test_get_span_bbox_with_characters_with_y_coord_outside_page_height(self):
        """Test get the bbox of a Span where the characters have negative x coordinates (OCR problem)."""
        document_bbox = {'1': {'text': 'e', 'x0': 0, 'x1': 1, 'y0': 301, 'y1': 302, 'page_number': 1}}
        document = Document(project=self.project, category=self.category, text='hello', bbox=document_bbox)
        span = Span(start_offset=1, end_offset=2)
        _ = Annotation(document=document, spans=[span], label=self.label, label_set=self.label_set)
        _ = Page(id_=1, number=1, original_size=(595.2, 300.0), document=document, start_offset=0, end_offset=3)
        with pytest.raises(ValueError, match='exceeds height of Page 0'):
            span.bbox()

    def test_get_span_bbox_with_unavailable_characters(self):
        """Test get the bbox of a Span where the characters are unavailable."""
        document_bbox = {
            '0': {'x0': 0, 'x1': 1, 'y0': 0, 'y1': 1, 'top': 10, 'bottom': 11, 'page_number': 1},
            '2': {'x0': 1, 'x1': 2, 'y0': 0, 'y1': 1, 'top': 10, 'bottom': 11, 'page_number': 1},
        }
        document = Document(project=self.project, category=self.category, text='hello', bbox=document_bbox)
        span = Span(start_offset=1, end_offset=2)
        _ = Annotation(document=document, spans=[span], label=self.label, label_set=self.label_set)

        span.bbox()
        # with self.assertRaises(ValueError) as context:
        # raise ValueError
        # todo find a way to raise a value error for characters, but ignore special Characters that
        #  do not provide a Bbox
        # assert 'does not have available characters bounding boxes.' in context.exception

    def test_document_check_bbox_coordinates(self):
        """Test bbox check for coordinates with valid coordinates."""
        document_bbox = {
            '0': {'x0': 0, 'x1': 1, 'y0': 0, 'y1': 1, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'h'}
        }
        document = Document(project=self.project, category=self.category, text='h', bbox=document_bbox)
        _ = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=1)
        self.assertTrue(document.bboxes)

    def test_document_check_bbox_zero_height_allowed(self):
        """Test bbox check with zero height without strict validation."""
        document_bbox = {
            '0': {'x0': 0, 'x1': 2, 'y0': 0, 'y1': 0, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'h'}
        }
        document = Document(project=self.project, category=self.category, text='h', bbox=document_bbox)
        _ = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=1)
        self.assertTrue(document.bboxes)

    def test_document_check_bbox_zero_height_strict_validation(self):
        """Test bbox check with zero height with strict validation, which does not allow it."""
        document_bbox = {
            '0': {'x0': 0, 'x1': 2, 'y0': 0, 'y1': 0, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'h'}
        }
        document = Document(
            project=self.project, category=self.category, text='h', bbox=document_bbox, strict_bbox_validation=True
        )
        _ = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=1)
        with pytest.raises(ValueError, match='has no height'):
            document.bboxes

    def test_document_check_bbox_zero_width_allowed(self):
        """Test bbox check with zero width without strict validation."""
        document_bbox = {
            '0': {'x0': 0, 'x1': 0, 'y0': 0, 'y1': 2, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'h'}
        }
        document = Document(project=self.project, category=self.category, text='h', bbox=document_bbox)
        _ = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=1)
        self.assertTrue(document.bboxes)

    def test_document_check_bbox_zero_width_strict_validation(self):
        """Test bbox check with zero width with strict validation, which does not allow it."""
        document_bbox = {
            '0': {'x0': 0, 'x1': 0, 'y0': 0, 'y1': 2, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'h'}
        }
        document = Document(
            project=self.project, category=self.category, text='h', bbox=document_bbox, strict_bbox_validation=True
        )
        _ = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=1)
        with pytest.raises(ValueError, match='has no width'):
            document.bboxes

    def test_docs_with_same_bbox_hash(self):
        """Test that bbox insertion order doesn't change the hash of the bboxes in a document."""
        document1_bbox = {
            '0': {'x0': 0, 'x1': 1, 'y0': 0, 'y1': 2, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'h'},
            '1': {'x0': 1, 'x1': 2, 'y0': 1, 'y1': 3, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'e'},
        }
        document1 = Document(project=self.project, category=self.category, text='hello', bbox=document1_bbox)
        _ = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document1, start_offset=0, end_offset=1)
        document1.set_text_bbox_hashes()
        document2_bbox = {
            '1': {'x0': 1, 'x1': 2, 'y0': 1, 'y1': 3, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'e'},
            '0': {'x0': 0, 'x1': 1, 'y0': 0, 'y1': 2, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'h'},
        }
        document2 = Document(project=self.project, category=self.category, text='hello', bbox=document2_bbox)
        _ = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document2, start_offset=0, end_offset=1)
        document2.set_text_bbox_hashes()
        assert document1._bbox_hash == document2._bbox_hash

    def test_document_text_modified(self):
        """Test that we can detect changes in the text of a document."""
        document_bbox = {
            '0': {'x0': 0, 'x1': 1, 'y0': 0, 'y1': 2, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'h'}
        }
        document = Document(
            project=self.project, category=self.category, text='hello', bbox=document_bbox, strict_bbox_validation=True
        )
        _ = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=1)
        self.assertTrue(document.text)
        document.set_text_bbox_hashes()
        self.assertFalse(document._check_text_or_bbox_modified())
        document._text = "123" + document.text
        self.assertTrue(document._check_text_or_bbox_modified())

    def test_document_bbox_modified(self):
        """Test that we can detect changes in the bboxes of a document."""
        document_bbox = {
            '0': {'x0': 0, 'x1': 1, 'y0': 0, 'y1': 2, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'h'}
        }
        document = Document(
            project=self.project, category=self.category, text='hello', bbox=document_bbox, strict_bbox_validation=True
        )
        page = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=1)
        self.assertTrue(document.bboxes)
        document.set_text_bbox_hashes()
        self.assertFalse(document._check_text_or_bbox_modified())
        document._characters[1] = Bbox(x0=1, x1=2, y0=1, y1=3, page=page, strict_validation=True)
        self.assertTrue(document._check_text_or_bbox_modified())

    def test_document_spans(self):
        """Test getting spans from a Document."""
        document = Document(project=self.project, category=self.category, text='p\n1\fnap2')
        span1 = Span(start_offset=0, end_offset=1)
        span2 = Span(start_offset=2, end_offset=3)
        span3 = Span(start_offset=4, end_offset=5)
        span4 = Span(start_offset=6, end_offset=8)

        _ = Annotation(
            document=document, is_correct=True, label=self.label, label_set=self.label_set, spans=[span1, span2]
        )
        _ = Annotation(document=document, is_correct=False, label=self.label, label_set=self.label_set, spans=[span3])
        _ = Annotation(document=document, is_correct=True, label=self.label, label_set=self.label_set, spans=[span4])

        assert len(document.spans()) == 4
        assert len(document.spans(use_correct=True)) == 3
        assert len(document.spans(start_offset=0, end_offset=4)) == 2
        assert len(document.spans(fill=True)) == 7
        assert len(document.spans(start_offset=4, end_offset=8, fill=True)) == 3

    def test_page_width(self):
        """Test width of Page."""
        document_bbox = {
            '0': {'x0': 0, 'x1': 1, 'y0': 0, 'y1': 2, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'h'}
        }
        document = Document(project=self.project, category=self.category, text='h', bbox=document_bbox)
        _ = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=1)
        assert document.get_page_by_index(0).width == 595.2

    def test_page_height(self):
        """Test height of Page."""
        document_bbox = {
            '0': {'x0': 0, 'x1': 1, 'y0': 0, 'y1': 2, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'h'}
        }
        document = Document(project=self.project, category=self.category, text='h', bbox=document_bbox)
        _ = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=1)
        assert document.get_page_by_index(0).height == 841.68

    def test_page_text(self):
        """Test text Page."""
        document_bbox = {
            '0': {'x0': 0, 'x1': 1, 'y0': 0, 'y1': 2, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'p'}
        }
        document = Document(project=self.project, category=self.category, text='page1\fpage2', bbox=document_bbox)
        _ = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=5)
        _ = Page(id_=2, number=2, original_size=(595.2, 841.68), document=document, start_offset=6, end_offset=11)
        assert document.get_page_by_index(0).text == 'page1'
        assert document.get_page_by_index(1).text == 'page2'

    def test_page_text_offsets(self):
        """Test text Page offsets."""
        document_bbox = {
            '0': {'x0': 0, 'x1': 1, 'y0': 0, 'y1': 2, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'p'}
        }
        document = Document(project=self.project, category=self.category, text='page1\fpage2', bbox=document_bbox)
        page1 = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=5)
        page2 = Page(id_=2, number=2, original_size=(595.2, 841.68), document=document, start_offset=6, end_offset=11)
        assert page1.text == document.text[page1.start_offset : page1.end_offset]
        assert page2.text == document.text[page2.start_offset : page2.end_offset]

    def test_page_get_bbox(self):
        """Test getting bbox for Page."""
        document_bbox = {
            '0': {'x0': 0, 'x1': 1, 'y0': 0, 'y1': 2, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'p'},
            '2': {'x0': 1, 'x1': 0, 'y0': 0, 'y1': 2, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': '1'},
            '8': {'x0': 0, 'x1': 1, 'y0': 10, 'y1': 12, 'top': 10, 'bottom': 11, 'page_number': 2, 'text': 'p'},
            '10': {'x0': 1, 'x1': 0, 'y0': 10, 'y1': 12, 'top': 10, 'bottom': 11, 'page_number': 2, 'text': '2'},
        }
        document = Document(project=self.project, category=self.category, text='p1\fp2', bbox=document_bbox)
        page1 = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=2)
        page2 = Page(id_=2, number=2, original_size=(595.2, 841.68), document=document, start_offset=3, end_offset=5)
        assert '0' in page1.get_bbox() and '2' in page1.get_bbox()
        assert '8' in page2.get_bbox() and '10' in page2.get_bbox()
        assert '0' not in page2.get_bbox() and '2' not in page2.get_bbox()
        assert '8' not in page1.get_bbox() and '10' not in page1.get_bbox()

    def test_page_annotations(self):
        """Test getting annotations of a Page."""
        document = Document(project=self.project, category=self.category, text='p\n1\fnap2')
        span1 = Span(start_offset=0, end_offset=1)
        span2 = Span(start_offset=2, end_offset=3)
        span3 = Span(start_offset=7, end_offset=9)

        page1 = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=3)
        page2 = Page(id_=2, number=2, original_size=(595.2, 841.68), document=document, start_offset=4, end_offset=8)

        annotation1 = Annotation(
            document=document, is_correct=True, label=self.label, label_set=self.label_set, spans=[span1, span2]
        )
        annotation2 = Annotation(
            document=document, is_correct=True, label=self.label, label_set=self.label_set, spans=[span3]
        )
        assert document.get_page_by_index(0).text == 'p\n1'
        assert document.get_page_by_index(1).text == 'nap2'
        assert annotation1 in document.annotations()
        assert annotation2 in document.annotations()
        assert annotation1 in page1.annotations()
        assert annotation2 in page2.annotations()
        assert annotation1 not in page2.annotations()
        assert annotation2 not in page1.annotations()
        assert page2.annotations(start_offset=4, end_offset=6) == []
        assert len(page2.annotations(start_offset=4, end_offset=6, fill=True)) == 1

    def test_page_spans(self):
        """Test getting spans from a Page."""
        document = Document(project=self.project, category=self.category, text='p\n1\fnap2')
        span1 = Span(start_offset=0, end_offset=1)
        span2 = Span(start_offset=2, end_offset=3)
        span3 = Span(start_offset=4, end_offset=5)
        span4 = Span(start_offset=6, end_offset=8)

        page1 = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=3)
        page2 = Page(id_=2, number=2, original_size=(595.2, 841.68), document=document, start_offset=4, end_offset=8)

        _ = Annotation(
            document=document, is_correct=True, label=self.label, label_set=self.label_set, spans=[span1, span2]
        )
        _ = Annotation(document=document, is_correct=False, label=self.label, label_set=self.label_set, spans=[span3])
        _ = Annotation(document=document, is_correct=True, label=self.label, label_set=self.label_set, spans=[span4])

        assert len(page1.spans()) == 2
        assert len(page2.spans()) == 2
        assert len(page2.spans(start_offset=7, end_offset=8)) == 1
        assert len(page2.spans(use_correct=True)) == 1
        page_2_spans = page2.spans(fill=True)
        assert len(page_2_spans) == 3
        filled_span = page_2_spans[1]
        assert filled_span.annotation.label.name == 'NO_LABEL'
        assert document.text[filled_span.start_offset : filled_span.end_offset] == 'a'

    def test_document_check_bbox_invalid_height_coordinates(self):
        """Test bbox check with invalid x coordinates regarding the page height."""
        document_bbox = {
            '0': {'x0': 1, 'x1': 0, 'y0': 0, 'y1': 2, 'top': 10, 'bottom': 11, 'page_number': 1, 'text': 'h'}
        }
        document = Document(project=self.project, category=self.category, text='h', bbox=document_bbox)
        _ = Page(id_=1, number=1, original_size=(595.2, 841.68), document=document, start_offset=0, end_offset=1)
        with pytest.raises(ValueError, match='has negative width'):
            document.bboxes

    def test_document_check_duplicated_annotations(self):
        """Test Annotations check when an error is raised due to duplicated Annotations by get_annotations."""
        # overwriting get_annotations for test
        class DocumentDuplicatedAnnotations(Document):
            def get_annotations(self):
                raise ValueError("is a duplicate of.")

        document = DocumentDuplicatedAnnotations(project=self.project, category=self.category, text="hello")
        self.assertFalse(document.check_annotations())

    def test_document_check_category_annotations(self):
        """Test Annotations check when an error is raised due to an incorrect Category by get_annotations."""
        # overwriting get_annotations for test
        class DocumentIncorrectCategoryAnnotations(Document):
            def get_annotations(self):
                raise ValueError("related to.")

        document = DocumentIncorrectCategoryAnnotations(project=self.project, category=self.category, text="hello")
        self.assertFalse(document.check_annotations())

    def test_to_there_must_not_be_a_folder(self):
        """Check that a virtual Document has now folder."""
        assert not os.path.isdir(self.document.document_folder)

    def test_new_annotation_in_annotation_set_of_document_of_add_foreign_annotation_set(self):
        """Add new annotation to a document."""
        project = Project(id_=None)
        document = Document(project=project, category=self.category)
        span = Span(start_offset=1, end_offset=2)

        with self.assertRaises(IndexError) as context:
            _ = Annotation(
                document=document,
                is_correct=True,
                label=self.label,
                annotation_set=self.annotation_set,
                label_set=self.label_set,
                spans=[span],
            )
            assert 'Annotation Set None is not part of Document None' in context.exception

    def test_new_annotation_in_document(self):
        """Add new annotation to a document."""
        project = Project(id_=None)
        document = Document(project=project, category=self.category)
        span = Span(start_offset=1, end_offset=2)
        annotation_set = AnnotationSet(document=document, label_set=self.label_set)

        annotation = Annotation(
            document=document,
            is_correct=True,
            label=self.label,
            annotation_set=annotation_set,
            label_set=self.label_set,
            spans=[span],
        )

        assert annotation in document.annotations()

    def test_new_annotation_in_document_with_confidence_zero(self):
        """Add new annotation to a document with confidence of 0.0."""
        project = Project(id_=None)
        document = Document(project=project, category=self.category)
        span = Span(start_offset=1, end_offset=2)
        annotation_set = AnnotationSet(document=document, label_set=self.label_set)

        annotation = Annotation(
            document=document,
            label=self.label,
            annotation_set=annotation_set,
            label_set=self.label_set,
            spans=[span],
            confidence=0.0,
        )

        assert annotation in document.annotations(use_correct=False)

    def test_new_annotation_in_annotation_set_of_document(self):
        """Add new annotation to a document."""
        project = Project(id_=None)
        document = Document(project=project, category=self.category)
        span = Span(start_offset=1, end_offset=2)
        annotation_set = AnnotationSet(document=document, label_set=self.label_set)

        annotation = Annotation(
            document=document,
            is_correct=True,
            label=self.label,
            annotation_set=annotation_set,
            label_set=self.label_set,
            spans=[span],
        )

        assert annotation in annotation_set.annotations

    def test_create_document_with_page_object(self):
        """Create a Document with pages information from a Page object."""
        document = Document(project=self.project, category=self.category, text='a')
        page_list = [{"id_": 1, "number": 1, "original_size": [595.2, 841.68]}]
        page = Page(**page_list[0], document=document, start_offset=0, end_offset=1)

        assert len(document.pages()) == len(page_list)
        assert page.image is None
        assert page.number == 1
        assert page.width == 595.2

    def test_create_new_annotation_set_in_document(self):
        """Add new annotation set to a document."""
        document = Document(project=self.project, category=self.category)
        annotation_set = AnnotationSet(document=document, label_set=self.label_set)
        assert annotation_set in document.annotation_sets()

    def test_to_add_two_spans_to_annotation(self):
        """Add one Span to one Annotation."""
        document = Document(project=self.project, category=self.category)
        span = Span(start_offset=1, end_offset=2)
        with self.assertRaises(ValueError) as context:
            Annotation(document=document, spans=[span, span], label=self.label, label_set=self.label_set)
            assert 'is a duplicate and will not be added' in context.exception

    def test_to_add_annotation_set_of_another_document(self):
        """One Annotation Set must only belong to one document."""
        document = Document(project=self.project, category=self.category)
        with self.assertRaises(ValueError):
            document.add_annotation_set(self.annotation_set)

    def test_to_add_annotation_to_none_category_document(self):
        """A Document with Category None must not contain Annotations."""
        document = Document(project=self.project)
        annotation_set = AnnotationSet(document=document, label_set=self.label_set)

        # Add annotation for the first time
        span = Span(start_offset=1, end_offset=2)
        with self.assertRaises(ValueError) as context:
            _ = Annotation(
                document=document,
                is_correct=True,
                label=self.label,
                annotation_set=annotation_set,
                label_set=self.label_set,
                spans=[span],
            )
            assert 'where the category is None' in context.exception

    def test_add_overlapping_virtual_annotations(self):
        """Add one Span as Annotation multiple times when document.id_ is None."""
        document = Document(project=self.project, category=self.category, data_file_name='add_twice.pdf')
        span = Span(start_offset=1, end_offset=2)
        annotation_set = AnnotationSet(document=document, label_set=self.label_set)
        Annotation(
            document=document,
            is_correct=True,
            label=self.label,
            annotation_set=annotation_set,
            label_set=self.label_set,
            spans=[span],
        )

        # Add annotation for the second time, heere it should be skipped.
        span = Span(start_offset=1, end_offset=2)
        with self.assertRaises(ValueError) as context:
            Annotation(
                document=document,
                is_correct=True,
                label=self.label,
                annotation_set=annotation_set,
                label_set=self.label_set,
                spans=[span],
            )
            assert "is a duplicate of" in context.exception

    def test_to_add_an_annotation_twice_to_a_document(self):
        """Test to add the same Annotation twice to a Document."""
        document = Document(project=self.project, category=self.category)
        span = Span(start_offset=1, end_offset=2)
        annotation = Annotation(document=document, spans=[span], label=self.label, label_set=self.label_set)
        with self.assertRaises(ValueError):
            document.add_annotation(annotation)
        self.assertEqual([annotation], document.annotations(use_correct=False))

    def test_to_add_annotation_with_same_span_offsets_and_label_to_a_document(self):
        """Test to add Annotation with a Span with the same offsets and same Label and Label Set to a Document."""
        document = Document(project=self.project, category=self.category)
        span_1 = Span(start_offset=1, end_offset=2)
        _ = Annotation(id_=1, document=document, spans=[span_1], label=self.label, label_set=self.label_set)
        span_2 = Span(start_offset=1, end_offset=2)
        assert span_1 == span_2
        with self.assertRaises(ValueError):
            _ = Annotation(id_=2, document=document, spans=[span_2], label=self.label, label_set=self.label_set)

    def test_to_add_annotation_with_same_span_offsets_but_different_label_to_a_document(self):
        """
        Test to add Annotation with a Span with the same offsets but different Label to a Document.

        Both Annotations have is_correct=False.
        """
        document = Document(project=self.project, category=self.category)
        label = Label(project=self.project, text='Second Offline Label', label_sets=[self.label_set])
        span_1 = Span(start_offset=1, end_offset=2)
        _ = Annotation(id_=1, document=document, spans=[span_1], label=self.label, label_set=self.label_set)
        span_2 = Span(start_offset=1, end_offset=2)
        _ = Annotation(id_=2, document=document, spans=[span_2], label=label, label_set=self.label_set)

    def test_to_add_two_annotations_to_a_document(self):
        """Test to add the same Annotation twice to a Document."""
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        second_span = Span(start_offset=1, end_offset=2)
        third_span = Span(start_offset=2, end_offset=3)
        first_annotation = Annotation(document=document, spans=[first_span], label_set=self.label_set, label=self.label)
        second_annotation = Annotation(
            document=document, spans=[second_span, third_span], label_set=self.label_set, label=self.label
        )
        self.assertEqual([first_annotation, second_annotation], document.annotations(use_correct=False))

    def test_to_return_a_custom_offset_string(self):
        """Test to return a offset string which was human edited on the Server."""
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        annotation = Annotation(
            document=document,
            spans=[first_span],
            label_set=self.label_set,
            label=self.label,
            is_correct=True,
            custom_offset_string=True,
            offset_string="custom_string",
        )
        assert annotation.offset_string == 'custom_string'

    def test_to_add_a_correct_annotation_with_a_duplicated_span_to_a_document(self):
        """Test to Span that has the same start and end offsets to a correct Annotation.

        24.06.2022: It's now allowed to have this operation. As one Annotation spanning only one Span is not
        identical with another Annotation with the same label but one additional Span.

        Example:
            A Document contains the text "My name is Manfred Meister": It should be possible to have one Annotation
            with Name: Span: "Manfred" and one Annotation with Name: Span "Manfred" and Span "Müller" as both
            Annotation should have a different confidence.

        """
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        second_span = Span(start_offset=1, end_offset=2)
        third_span = Span(start_offset=2, end_offset=3)
        _ = Annotation(
            document=document, spans=[first_span], label_set=self.label_set, label=self.label, is_correct=True
        )

        _ = Annotation(
            document=document,
            spans=[second_span, third_span],
            label_set=self.label_set,
            label=self.label,
            is_correct=True,
        )

        # todo: check if Spans are related to the Document and Annotations are just linked where one Span can relate to
        #    many Annotations.
        # with self.assertRaises(ValueError) as context:
        #    assert 'Span can relate to multiple Annotations but is unique in a Document' in context.exception

    def test_to_reuse_spans_across_correct_annotations(self):
        """Test if we find inconsistencies when one Span is assigned to a new correct Annotation."""
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        second_span = Span(start_offset=2, end_offset=3)
        Annotation(document=document, spans=[first_span], label_set=self.label_set, label=self.label, is_correct=True)

        Annotation(
            document=document,
            spans=[first_span, second_span],
            label_set=self.label_set,
            label=self.label,
            is_correct=True,
        )

    def test_to_reuse_spans_across_annotations(self):
        """Test if we find inconsistencies when one Span is assigned to a new Annotation."""
        document = Document(project=self.project, category=self.category)
        first_span = Span(start_offset=1, end_offset=2)
        second_span = Span(start_offset=2, end_offset=3)
        Annotation(document=document, spans=[first_span], label_set=self.label_set, label=self.label)
        Annotation(document=document, spans=[first_span, second_span], label_set=self.label_set, label=self.label)
        assert len(document.annotations(use_correct=False)) == 2

    def test_lose_weight(self):
        """Lose weight should remove session and documents."""
        project = Project(id_=None)
        _ = Category(project=project)
        label_set = LabelSet(project=project)
        Label(project=project, label_sets=[label_set])
        project.lose_weight()
        assert project.session is None
        assert project.categories[0].session is None
        assert project.label_sets[0].session is None
        assert project.labels[0].session is None
        assert project.virtual_documents == []
        assert project.documents == []
        assert project.test_documents == []


class TestSeparateLabels(unittest.TestCase):
    """Test the feature create separated Labels per Label Set."""

    @unittest.skip(reason='Feature needed')
    def test_normal_setup(self):
        """Labels are initialized by the Project can be reused by Label Sets."""
        raise NotImplementedError

    @unittest.skip(reason='Feature needed')
    def test_separat_setup(self):
        """Labels are initialized by the Project cannot be reused by Label Sets."""
        raise NotImplementedError


class TestKonfuzioDataCustomPath(unittest.TestCase):
    """Test handle data."""

    def test_get_text_for_doc_needing_update(self):
        """Test to load the Project into a custom folder and only get one document."""
        prj = Project(id_=TEST_PROJECT_ID, project_folder='my_own_data')
        doc = prj.get_document_by_id(214414)
        doc.download_document_details()
        self.assertTrue(is_file(doc.txt_file_path))
        for document in prj.documents:
            if document.id_ != doc.id_:
                self.assertTrue(not is_file(document.txt_file_path, raise_exception=False))
        self.assertTrue(doc.text)
        prj.delete()

    def test_make_sure_text_is_downloaded_automatically(self):
        """Test if a Text is downloaded automatically."""
        prj = Project(id_=TEST_PROJECT_ID, project_folder='my_own_data')
        doc = prj.get_document_by_id(214414)
        self.assertFalse(is_file(doc.txt_file_path, raise_exception=False))
        self.assertEqual(None, doc._text)
        self.assertTrue(doc.text)
        self.assertTrue(is_file(doc.txt_file_path))
        prj.delete()


class TestKonfuzioOneVirtualTwoRealCategories(unittest.TestCase):
    """Test handle data."""

    @classmethod
    def setUpClass(cls) -> None:
        """Initialize the test Project."""
        cls.project = Project(id_=None, project_folder=OFFLINE_PROJECT)
        category = Category(project=cls.project, name_clean='Virtual Category')
        label = Label(name='Only virtual Category Label', project=cls.project)
        _ = LabelSet(project=cls.project, is_default=False, labels=[label], categories=[category])

    def test_get_labels_of_virtual_category(self):
        """Return only related Labels as Information Extraction can be trained per Category."""
        assert len(self.project.categories[-1].labels) == 1  # virtual created Categories have no NO_LABEL

    def test_get_labels_of_category(self):
        """Return only related Labels as Information Extraction can be trained per Category."""
        real_category1 = self.project.get_category_by_id(TEST_PAYSLIPS_CATEGORY_ID)
        real_category2 = self.project.get_category_by_id(TEST_RECEIPTS_CATEGORY_ID)
        # we calculate the set to avoid double counting the NO_LABEL
        assert len(set(real_category1.labels + real_category2.labels)) == len(self.project.labels) - 1


class TestKonfuzioDataSetup(unittest.TestCase):
    """Test handle data."""

    document_count = 51
    test_document_count = 4
    annotations_correct = 24
    # 24 created by human
    # https://app.konfuzio.com/admin/server/sequenceannotation/?
    # document__dataset_status__exact=2&label__id__exact=867&project=46&status=3
    # 1 Created by human and revised by human, but on a document that has no category
    # https://app.konfuzio.com/admin/server/sequenceannotation/?
    # document__dataset_status__exact=2&label__id__exact=867&project=46&status=1

    @classmethod
    def setUpClass(cls) -> None:
        """Initialize the test Project."""
        cls.prj = Project(id_=None, project_folder=OFFLINE_PROJECT)

    def test_number_training_documents(self):
        """Test the number of Documents in data set status training."""
        assert len(self.prj.documents) == self.document_count

    def test_get_labels_of_category(self):
        """Return only related Labels as Information Extraction can be trained per Category."""
        # we calculate the set to avoid double counting the NO_LABEL
        assert len(set(self.prj.categories[0].labels + self.prj.categories[1].labels)) == len(self.prj.labels)

    def test_document_with_no_category_must_have_no_annotations(self):
        """Test if we skip Annotations in no Category Documents."""
        document = self.prj.get_document_by_id(44864)
        assert document.category is None
        with self.assertRaises(ValueError) as context:
            document.annotations()
            assert 'where the category is None' in context.exception

    def test_number_test_documents(self):
        """Test the number of Documents in data set status test."""
        assert len(self.prj.test_documents) == self.test_document_count

    def test_number_excluded_documents(self):
        """Test the number of Documents in data set status excluded."""
        assert len(self.prj.excluded_documents) == 1

    def test_all_labels_have_threshold(self):
        """Test that all labels have the attribute threshold."""
        for label in self.prj.labels:
            assert hasattr(label, 'threshold')

    def test_number_preparation_documents(self):
        """Test the number of Documents in data set status preparation."""
        assert len(self.prj.preparation_documents) == 0

    def test_annotation_of_label(self):
        """Test the number of Annotations across all Documents in training."""
        label = self.prj.get_label_by_id(867)
        annotations = label.annotations(categories=[self.prj.get_category_by_id(63)])
        assert len(annotations) == self.annotations_correct

    def test_annotation_hashable(self):
        """Test if an annotation can be hashed."""
        set(self.prj.get_document_by_id(TEST_DOCUMENT_ID).annotations())

    def test_get_all_spans_of_a_document(self):
        """Test to get all Spans in a Document."""
        # Before we had 21 Spans after the a code change to allow overlapping Annotations we have 23 Spans
        # due to the fact that one Span is not identical, so one Annotation relates to one Span.
        # One more for a total of 24 since we are not filtering out the rejected annotations.
        assert len(self.prj.get_document_by_id(TEST_DOCUMENT_ID).spans()) == 24

    def test_span_hashable(self):
        """Test if a Span can be hashed."""
        annotation = self.prj.get_document_by_id(TEST_DOCUMENT_ID).annotations()[0]
        set(annotation.spans)

    def test_number_of_label_sets(self):
        """Test Label Sets numbers."""
        # Online Label Sets + added during tests +  no_label_set
        assert len(self.prj.label_sets) == 13

    # def test_check_tokens(self):
    #     """Test to find not matched Annotations."""
    #     category = self.prj.get_category_by_id(63)
    #     spans = self.prj.get_label_by_id(867).check_tokens(categories=[category])
    #     assert len(spans) == 25

    def test_has_multiple_annotation_sets(self):
        """Test Label Sets in the test Project."""
        assert self.prj.get_label_set_by_name('Brutto-Bezug').has_multiple_annotation_sets

    def test_has_not_multiple_annotation_sets(self):
        """Test Label Sets in the test Project."""
        assert not self.prj.get_label_set_by_name('Lohnabrechnung').has_multiple_annotation_sets

    def test_default_label_set(self):
        """Test the main Label Set incl. it's labels."""
        default_label_set = self.prj.get_label_set_by_name('Lohnabrechnung')
        assert default_label_set.labels.__len__() == 10

    def test_to_filter_annotations_by_label(self):
        """Test to get correct Annotations of a Label."""
        label = self.prj.get_label_by_id(858)
        annotations = label.annotations(categories=[self.prj.get_category_by_id(63)])
        self.assertEqual(len(annotations), self.annotations_correct + 1)

    def test_category(self):
        """Test if Category of main Label Set is initialized correctly."""
        assert len(self.prj.categories) == 2
        assert self.prj.categories[0].id_ == 63
        assert self.prj.label_sets[0].categories[0].id_ == 63

    def test_category_documents(self):
        """Test category of Documents associated to a Category."""
        category = self.prj.get_category_by_id(63)
        category_documents = category.documents()

        assert len(category_documents) == 25
        for document in category_documents:
            assert document.category == category

    def test_category_test_documents(self):
        """Test category of Test Documents associated to a Category."""
        category = self.prj.get_category_by_id(63)
        category_test_documents = category.test_documents()

        assert len(category_test_documents) == 3
        for document in category_test_documents:
            assert document.category == category

    def test_category_annotations_by_label(self):
        """Test getting Annotations of a Category by Labels."""
        category = self.prj.get_category_by_id(63)
        category_label_sets = category.label_sets
        label = category_label_sets[0].labels[0]
        for annotation in label.annotations(categories=[category]):
            if annotation.document.category is not None:
                assert annotation.document.category == category

    def test_category_annotations_by_document(self):
        """Test getting Annotations of a Category by Documents."""
        category = self.prj.get_category_by_id(63)
        for document in category.documents():
            for annotation in document.annotations():
                if not annotation.label_set.is_default:
                    assert annotation.label_set in category.label_sets

    def test_label_sets_of_category(self):
        """Test Label Sets of a Category."""
        category = self.prj.get_category_by_id(63)
        category_label_sets = category.label_sets

        assert len(category_label_sets) > 0
        for label_set in category_label_sets:
            assert category in label_set.categories

    def test_labels_of_category(self):
        """Test Labels of a Category."""
        category = self.prj.get_category_by_id(63)
        with self.assertRaises(AttributeError) as context:
            category.labels
            assert "'Category' object has no attribute 'labels'" in context.exception

    def test_label_sets_of_label(self):
        """Test Label Sets of a Label."""
        label: Label = self.prj.get_label_by_id(861)  # Lohnart
        self.assertEqual(2, len(label.label_sets))

    def test_label_set_multiple(self):
        """Test Label Set config that is set to multiple."""
        label_set = self.prj.get_label_set_by_name('Brutto-Bezug')
        assert label_set.categories.__len__() == 1

    def test_number_of_labels_of_label_set(self):
        """Test the number of Labels of the default Label Set."""
        label_set = self.prj.get_label_set_by_name('Lohnabrechnung')
        # assert label_set.categories == [self.prj.get_category_by_id(label_set.id_)]  # defines a category
        assert label_set.labels.__len__() == 10

    def test_categories(self):
        """Test get Labels in the Project."""
        assert self.prj.categories.__len__() == 2
        payslips_category = self.prj.get_category_by_id(TEST_PAYSLIPS_CATEGORY_ID)
        assert payslips_category.name == 'Lohnabrechnung'
        # We have 5 Label Sets, Lohnabrechnung is Category and a Label Set as it hold labels, however a Category
        # cannot hold labels
        assert sorted([label_set.name for label_set in self.prj.categories[0].label_sets]) == [
            'Brutto-Bezug',
            'Lohnabrechnung',
            'NO_LABEL_SET',
            'Netto-Bezug',
            'Steuer',
            'Verdiensibescheinigung',
        ]
        receipts_category = self.prj.get_category_by_id(TEST_RECEIPTS_CATEGORY_ID)
        assert receipts_category.name == 'Quittung (GERMAN)'
        # We have 5 Label Sets, Quittung is Category and a Label Set as it hold labels, however a Category
        # cannot hold labels
        assert sorted([label_set.name for label_set in self.prj.categories[0].label_sets]) == [
            'Brutto-Bezug',
            'Lohnabrechnung',
            'NO_LABEL_SET',
            'Netto-Bezug',
            'Steuer',
            'Verdiensibescheinigung',
        ]

    def test_get_images(self):
        """Test get paths to the images of the first training document."""
        document = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        assert len(document.pages()) == 1

    def test_get_file(self):
        """Test get path to the file of the first training document."""
        self.prj.documents[0].get_file()
        assert self.prj.documents[0].ocr_file_path

    def test_get_file_without_ocr(self):
        """Download file without OCR."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        doc.get_file(ocr_version=False)
        is_file(doc.file_path)

    def test_get_file_with_ocr(self):
        """Download file without OCR."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        doc.get_file()
        is_file(doc.ocr_file_path)

    @unittest.skip(reason='Server Issue https://gitlab.com/konfuzio/objectives/-/issues/9286')
    def test_make_sure_annotations_are_downloaded_automatically(self):
        """Test if Annotations are downloaded automatically."""
        prj = Project(id_=TEST_PROJECT_ID, project_folder='another')
        doc = prj.get_document_by_id(TEST_DOCUMENT_ID)
        self.assertFalse(is_file(doc.annotation_file_path, raise_exception=False))
        self.assertEqual(None, doc._annotations)
        self.assertTrue(doc.annotations())
        self.assertEqual(19, len(doc._annotations))
        self.assertTrue(is_file(doc.annotation_file_path))
        prj.delete()

    @unittest.skip(reason='Server Issue https://gitlab.com/konfuzio/objectives/-/issues/9286')
    def test_make_sure_annotation_sets_are_downloaded_automatically(self):
        """Test if Annotation Sets are downloaded automatically."""
        prj = Project(id_=TEST_PROJECT_ID, project_folder='another2')
        doc = prj.get_document_by_id(TEST_DOCUMENT_ID)
        self.assertFalse(is_file(doc.annotation_set_file_path, raise_exception=False))
        self.assertEqual(None, doc._annotation_sets)
        self.assertTrue(doc.annotation_sets())
        self.assertEqual(4, len(doc._annotation_sets))
        self.assertTrue(is_file(doc.annotation_set_file_path))
        prj.delete()

    def test_make_sure_pages_are_downloaded_automatically(self):
        """Test if Pages are downloaded automatically."""
        prj = Project(id_=TEST_PROJECT_ID, project_folder='another33')
        doc = prj.get_document_by_id(TEST_DOCUMENT_ID)
        self.assertFalse(is_file(doc.pages_file_path, raise_exception=False))
        self.assertEqual([], doc._pages)
        self.assertTrue(doc.pages())
        self.assertTrue(is_file(doc.pages_file_path))
        prj.delete()

    def test_add_label_set_without_category_to_document_with_category(self):
        """Test to add a Label Set without Category to a Document with a Category."""
        prj = Project(id_=TEST_PROJECT_ID)  # new init to not add data to self.prj
        doc = prj.get_document_by_id(TEST_DOCUMENT_ID)
        label = Label(project=prj)
        label_set = LabelSet(project=prj)
        with self.assertRaises(ValueError) as context:
            Annotation(document=doc, label_set=label_set, label=label)
            assert 'uses Label Set without Category' in context.exception

    def test_get_annotations_set_without_category_to_document_with_category(self):
        """Test to add a Label Set without Category to a Document with a Category."""
        prj = Project(id_=TEST_PROJECT_ID)  # new init to not add data to self.prj
        doc = prj.get_document_by_id(214414)
        with self.assertRaises(ValueError) as context:
            doc.annotations()
            assert 'Document without Category must not have Annotations' in context.exception

    def test_get_bbox(self):
        """Test to get BoundingBox of Text offset."""
        prj = Project(id_=TEST_PROJECT_ID)  # new init to not add data to self.prj
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        assert doc.category
        label_set = LabelSet(project=self.prj, categories=[doc.category])
        label = Label(project=prj)
        span = Span(start_offset=1, end_offset=2)
        annotation = Annotation(document=doc, label_set=label_set, label=label, spans=[span])
        span = Span(start_offset=44, end_offset=65, annotation=annotation)
        # only Character 60, 61 and 62 provide bounding boxes, all others are None
        span.bbox()
        self.assertEqual(span.page.index, 0)
        self.assertEqual(span.line_index, 0)
        self.assertEqual(span.bbox().x0, 426.0)
        self.assertEqual(span.bbox().x1, 442.8)
        self.assertEqual(span.bbox().y0, 808.831)
        self.assertEqual(span.bbox().y1, 817.831)
        self.assertEqual(span.bbox().area, 151.2)

    def test_size_of_project(self):
        """Test size of Project and compare it to the size after Documents have been loaded."""
        import sys
        from types import ModuleType, FunctionType
        from gc import get_referents

        # Custom objects know their class.
        # Function objects seem to know way too much, including modules.
        # Exclude modules as well.
        BLACKLIST = type, ModuleType, FunctionType

        def _getsize(obj):
            """Sum size of object & members. From https://stackoverflow.com/a/30316760."""
            if isinstance(obj, BLACKLIST):
                raise TypeError('getsize() does not take argument of type: ' + str(type(obj)))
            seen_ids = set()
            size = 0
            objects = [obj]
            while objects:
                need_referents = []
                for obj in objects:
                    if not isinstance(obj, BLACKLIST) and id(obj) not in seen_ids:
                        seen_ids.add(id(obj))
                        size += sys.getsizeof(obj)
                        need_referents.append(obj)
                objects = get_referents(*need_referents)
            return size

        # start of test
        prj = Project(id_=46)
        before = _getsize(prj)
        for document in prj.documents:
            document.text
        after = _getsize(prj)
        assert 1.7 < after / before < 1.8

        # strings in prj take slightly less space than in a list
        assert _getsize([doc.text for doc in prj.documents]) + before < after + 500

        # the text of the document is the only thing causing the size difference
        for document in prj.documents:
            document._text = None
        assert _getsize(prj) == before

    def test_create_new_doc_via_text_and_bbox(self):
        """Test to create a new Document which by a text and a bbox."""
        doc = Project(id_=None, project_folder=OFFLINE_PROJECT).get_document_by_id(TEST_DOCUMENT_ID)
        new_doc = Document(project=doc.project, text=doc.text, bbox=doc.get_bbox())
        assert new_doc.text
        assert new_doc.get_bbox()
        assert new_doc.number_of_pages == 1
        assert new_doc.number_of_lines == 70

    def test_category_of_document(self):
        """Test to download a file which includes a whitespace in the name."""
        category = Project(id_=None, project_folder=OFFLINE_PROJECT).get_document_by_id(44860).category
        self.assertEqual(category.name, 'Lohnabrechnung')

    def test_category_of_document_without_category(self):
        """Test the Category of a Document without Category."""
        category = Project(id_=None, project_folder=OFFLINE_PROJECT).get_document_by_id(44864).category
        self.assertIsNone(category)

    def test_get_file_with_white_colon_name(self):
        """Test to download a file which includes a whitespace in the name."""
        doc = Project(id_=None, project_folder=OFFLINE_PROJECT).get_document_by_id(44860)
        doc.get_file()

    def test_labels(self):
        """Test get Labels in the Project."""
        assert [label.name for label in sorted(self.prj.get_category_by_id(TEST_PAYSLIPS_CATEGORY_ID).labels)] == [
            'Austellungsdatum',
            'Auszahlungsbetrag',
            'Bank inkl. IBAN',
            'Betrag',
            'Bezeichnung',
            'EMPTY_LABEL',
            'Faktor',
            'Gesamt-Brutto',
            'Lohnart',
            'Menge',
            'NO_LABEL',  # Added for the Tokenizer
            'Nachname',
            'Netto-Verdienst',
            'Personalausweis',
            'Sozialversicherung',
            'Steuer-Brutto',
            'Steuerklasse',
            'Steuerrechtliche Abzüge',
            'Vorname',
        ]
        assert [label.name for label in sorted(self.prj.get_category_by_id(TEST_RECEIPTS_CATEGORY_ID).labels)] == [
            'Ansprechpartner',
            'Anzahl / Menge',
            'Artikelbezeichnung',
            'Artikelnummer',
            'Ausstelldatum',
            'Bedienung Nr',
            'BonNr',
            'Brutto (Ergebnis der MwSt. Berechnung)',
            'Einheit',
            'Einzelpreis (Brutto)',
            'Filial-/Markt-Nummer',
            'Firmenname',
            'Gesamtpreis (Brutto)',
            'Hausnummer',
            'Kassennummer',
            'Mehrwertsteuerbetrag',
            'Mehrwertsteuersatz',
            'NO_LABEL',
            'Netto (Basis der MwSt. Berechnung)',
            'Ort',
            'Postleitzahl',
            'Rechnungsnummer',
            'Referenz auf MwSt',
            'Steuernummer',
            'Straße',
            'Telefonnummer',
            'Uhrzeit',
            'Umsatzsteuer-Identifikationsnummer',
            'Währung',
            'Zahlungsmethode',
        ]

    def test_project(self):
        """Test basic properties of the Project object."""
        assert is_file(self.prj.meta_file_path)
        assert self.prj.documents[1].id_ > self.prj.documents[0].id_
        assert len(self.prj.documents)
        # check if we can initialize a new project object, which will use the same data
        assert len(self.prj.documents) == self.document_count
        new_project = Project(id_=None, project_folder=OFFLINE_PROJECT)
        assert len(new_project.documents) == self.document_count
        assert new_project.meta_file_path == self.prj.meta_file_path

    def test_update_prj(self):
        """Test number of Documents after updating a Project."""
        assert len(self.prj.documents) == self.document_count
        self.prj.get(update=True)
        assert len(self.prj.documents) == self.document_count
        is_file(self.prj.meta_file_path)

    @unittest.skip(reason='No update logic of project about new Annotation.')
    def test_annotations_in_document(self):
        """Test number and value of Annotations."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        assert len(doc.annotations(use_correct=False)) == 24
        assert doc.annotations()[0].offset_string == ['22.05.2018']  # start_offset=465, start_offset=466
        assert len(doc.annotations()) == 24
        assert doc.annotations()[0].is_online
        assert not doc.annotations()[0].save()  # Save returns False because Annotation is already online.

    def test_span_line_index_in_document(self):
        """Test line_index of span."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        label_set = LabelSet(project=self.prj, categories=[doc.category])
        label = Label(project=self.prj)
        span = Span(start_offset=1000, end_offset=1002)
        _ = Annotation(document=doc, label_set=label_set, label=label, spans=[span])
        assert span.page.index == 0
        assert span.line_index == 13

    def test_annotation_sets_in_document(self):
        """Test number of Annotation Sets in a specific Document in the test Project."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        assert len(doc.annotation_sets()) == 24  # After Update to use the TEST_DOCUMENT_ID

    def test_get_annotation_set_after_removal(self):
        """Test get an annotation set that no longer exists."""
        with self.assertRaises(IndexError) as _:
            # create annotation for a certain annotation set in a document
            doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)

            # get the annotation set ID of the first annotation
            annotations = doc.annotations()
            annotation_set_id = annotations[0].annotation_set.id_

            assert isinstance(annotation_set_id, int)

            # delete annotation set
            doc._annotation_sets = []

            # trying to get an annotation set that no longer exists
            _ = doc.get_annotation_set_by_id(annotation_set_id)

    def test_name_of_category(self):
        """Test the name of the Category."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        assert doc.category.name == 'Lohnabrechnung'

    def test_assignee_of_document(self):
        """Test Assignee of a Document."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        assert doc.assignee == 1043  # Document has Assignee ch+test@konfuzio.com with user ID 1043

    def test_add_document_twice(self):
        """Test adding same Document twice."""
        old_doc = self.prj.get_document_by_id(44834)
        with self.assertRaises(ValueError):
            self.prj.add_document(old_doc)
        assert len(self.prj.documents) == self.document_count

    def test_correct_annotations(self):
        """Test correct Annotations of a certain Label in a specific document."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        label = self.prj.get_label_by_id(867)
        assert len(doc.annotations(label=label)) == 1

    def test_annotation_start_offset_zero_filter(self):
        """Test Annotations with start offset equal to zero."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        assert len(doc.annotations()) == 19
        assert doc.annotations()[0].start_offset == 66

    def test_multiline_annotation(self):
        """Test to convert a multiline span Annotation to a dict."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        assert len(doc.annotations()[0].eval_dict) == 2

    def test_compare_dicts(self):
        """Test to convert a Annotation to a dict."""
        annotations = self.prj.documents[0].annotations()
        for annotation in annotations:
            if annotation.id_ == 4420022:
                span = annotation.spans[0]

        empty_span = Span(start_offset=0, end_offset=0)

        assert empty_span.eval_dict().keys() == span.eval_dict().keys()

    def test_annotation_to_dict(self):
        """Test to convert a Annotation to a dict."""
        anno = None
        annotations = self.prj.documents[0].annotations()
        for annotation in annotations:
            if annotation.id_ == 4420022:
                anno = annotation.eval_dict[0]

        assert anno is not None

        assert anno["confidence"] == 1.0
        assert anno["created_by"] == 59
        assert not anno["custom_offset_string"]
        assert anno["end_offset"] == 366
        assert anno["is_correct"]
        assert anno["label_id"] == 860  # original REST API calls it "label" however means label_id
        assert anno["label_threshold"] == 0.1
        assert anno["custom_offset_string"] is None
        assert not anno["revised"]
        assert anno["revised_by"] is None
        assert anno["annotation_set_id"] == 78730  # v2 REST API calls it still section
        assert anno["label_set_id"] == 63  # v2 REST API call it still section_label_id
        assert anno["start_offset"] == 365

        assert anno['page_width'] == 595.2
        assert anno['page_height'] == 841.68
        assert anno['x0'] == 126.96
        assert anno['x1'] == 131.04
        assert anno['y0'] == 772.589
        assert anno['y1'] == 783.589
        assert anno['x0_relative'] == 0.2133064516129032
        assert anno['x1_relative'] == 0.2201612903225806
        assert anno['y0_relative'] == 0.9179129835566963
        assert anno['y1_relative'] == 0.9309820834521435
        assert anno['line_index'] == 4
        assert anno['page_index'] == 0
        assert anno['page_index_relative'] == 0

    def test_document_annotations_filter(self):
        """Test Annotations filter."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        self.assertEqual(len(doc.annotations()), 19)
        assert len(doc.annotations(label=self.prj.get_label_by_id(858))) == 1
        assert len(doc.annotations(use_correct=False)) == 22  # 21 if not considering negative ones

    def test_document_offset(self):
        """Test Document offsets."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        assert doc.annotations()[0].offset_string == ['328927/10103', '22.05.2018']

    def test_document_id_when_loading_from_disk(self):
        """Test ID of Document."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        assert doc.id_ == TEST_DOCUMENT_ID

    def test_document_check_bbox_available(self):
        """Test deepcopy will copy over Bbox."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        virtual_doc = deepcopy(doc)
        assert virtual_doc.bboxes

    def test_document_check_bbox(self):
        """Test bbox check."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        virtual_doc = deepcopy(doc)
        self.assertTrue(virtual_doc.bboxes)
        virtual_doc.set_text_bbox_hashes()
        virtual_doc._text = '123' + doc.text  # Change text to bring bbox out of sync.
        with pytest.raises(ValueError, match='Bbox provides Character "n" document text refers to "l"'):
            virtual_doc.check_bbox()

    def test_hashing_bboxes_faster_than_recalculation(self):
        """Test that it's 100x faster to compare hashes of text and bboxes rathar than force recalculation of bboxes."""
        import time

        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        virtual_doc = deepcopy(doc)
        virtual_doc.bboxes

        t0 = time.monotonic()
        for _ in range(100):
            virtual_doc._check_text_or_bbox_modified()
            virtual_doc.bboxes
        t_hash = time.monotonic() - t0

        t0 = time.monotonic()
        for _ in range(100):
            virtual_doc.check_bbox()
        t_recalculate = time.monotonic() - t0

        assert t_hash / t_recalculate < 1 / 100

    @unittest.skip(reason='Waiting for API to support to add to default Annotation Set')
    def test_document_add_new_annotation(self):
        """Test adding a new annotation."""
        doc = self.prj.labels[0].documents[5]  # the latest document
        # we create a revised Annotations, as only revised Annotation can be deleted
        # if we would delete an unrevised annotation, we would provide feedback and thereby keep the
        # Annotation as "wrong" but "revised"
        assert len(doc.annotations(use_correct=False)) == 23
        label = self.prj.labels[0]
        new_anno = Annotation(
            start_offset=225,
            end_offset=237,
            label=label.id_,
            label_set_id=None,  # hand selected document section label
            revised=True,
            is_correct=True,
            accuracy=0.98765431,
            document=doc,
        )
        # make sure Document Annotations are updated too
        assert len(doc.annotations(use_correct=False)) == 24
        label = self.prj.labels[0]
        annotations = label.annotations(categories=[self.prj.get_category_by_id(63)])
        self.assertEqual(self.document_count + 1, len(annotations))
        assert new_anno.id_ is None
        new_anno.save()
        assert new_anno.id_
        new_anno.delete()
        assert new_anno.id_ is None
        assert len(doc.annotations(use_correct=False)) == 13
        annotations = label.annotations(categories=[self.prj.get_category_by_id(63)])
        self.assertEqual(self.document_count, len(annotations))

    @unittest.skip(reason="Skip: Changes in Trainer Annotation needed to require a Label for every Annotation init.")
    def test_document_add_new_annotation_without_label(self):
        """Test adding a new annotation."""
        with self.assertRaises(AttributeError) as _:
            _ = Annotation(
                start_offset=225,
                end_offset=237,
                label=None,
                label_set_id=0,  # hand selected document section label
                revised=True,
                is_correct=True,
                accuracy=0.98765431,
                document=Document(),
            )
        # TODO: expand assert to check for specific error message

    @unittest.skip(reason="Skip: Changes in Trainer Annotation needed to require a Document for every Annotation init.")
    def test_init_annotation_without_document(self):
        """Test adding a new annotation."""
        with self.assertRaises(AttributeError) as _:
            _ = Annotation(
                start_offset=225,
                end_offset=237,
                label=None,
                label_set_id=0,
                revised=True,
                is_correct=True,
                accuracy=0.98765431,
                document=None,
            )

        # TODO: expand assert to check for specific error message

    @pytest.mark.xfail(reason='We cannot define the Annotation Set automatically.')
    def test_init_annotation_with_default_annotation_set(self):
        """Test adding a new Annotation without providing the Annotation Set."""
        prj = Project(id_=TEST_PROJECT_ID)
        doc = Document(project=prj)
        annotation = Annotation(
            start_offset=225,
            end_offset=237,
            label=prj.labels[0],
            label_set_id=0,
            revised=True,
            is_correct=True,
            accuracy=0.98765431,
            document=doc,
            annotation_set=None,
        )

        # an Annotation Set needs to be created or retrieved after the Annotation is saved
        assert annotation.annotation_set.id_ == 78730

    def test_bio_scheme_saving_and_loading(self):
        """Test if generated bio scheme list is identical to loaded from file."""
        doc = self.prj.documents[0]
        bio_annotations1 = doc.get_text_in_bio_scheme(update=True)
        bio_annotations2 = doc.get_text_in_bio_scheme(update=False)

        assert bio_annotations1 == bio_annotations2

    @unittest.skip(reason="Issue https://gitlab.com/konfuzio/objectives/-/issues/8664.")
    def test_get_text_in_bio_scheme(self):
        """Test getting Document in the BIO scheme."""
        doc = self.prj.documents[0]
        bio_annotations = doc.get_text_in_bio_scheme()
        assert len(bio_annotations) == 398
        # check for multiline support in bio schema
        assert bio_annotations[1][0] == '328927/10103/00104'
        assert bio_annotations[1][1] == 'B-Austellungsdatum'
        assert bio_annotations[8][0] == '22.05.2018'
        assert bio_annotations[8][1] == 'B-Austellungsdatum'

    def test_number_of_all_documents(self):
        """Count the number of all available documents online."""
        project = Project(id_=None, project_folder=OFFLINE_PROJECT)
        assert len(project._documents) == 57

    def test_create_empty_annotation(self):
        """
        Create an empty Annotation and get the start offset.

        The empty annotation should be added to the document as this represents the way the tokenizer
        creates empty annotations.
        """
        prj = Project(id_=TEST_PROJECT_ID)
        label = Label(project=prj)
        doc = Document(text='', project=prj, category=prj.get_category_by_id(63))
        label_set = LabelSet(project=prj, categories=[prj.get_category_by_id(63)])
        span = Span(start_offset=1, end_offset=2)
        annotation_set = AnnotationSet(document=doc, label_set=label_set)
        _ = Annotation(label=label, annotation_set=annotation_set, label_set=label_set, document=doc, spans=[span])

    def test_get_annotations_for_offset_of_first_and_last_name(self):
        """Get Annotations for all offsets in the document."""
        doc = self.prj.get_document_by_id(TEST_DOCUMENT_ID)
        filtered_annotations = doc.annotations(start_offset=1500, end_offset=1530)
        self.assertEqual(len(filtered_annotations), 3)  # 3 is correct even 4 Spans!
        text = '198,34\n  Erna-Muster Eiermann                         KiSt      15,83   Solz        10,89\n  '
        self.assertEqual(doc.text[1498:1590], text)

    def test_create_list_of_regex_for_label_without_annotations(self):
        """Check regex build for empty Labels."""
        category = self.prj.get_category_by_id(63)
        label = next(x for x in self.prj.labels if len(x.annotations(categories=[category])) == 0)
        automated_regex_for_label = label.regex(categories=[category])
        # There is no regex available.
        assert len(automated_regex_for_label) == 0

    @unittest.skip(reason='Patch not supported by Text-Annotation Server.')
    def test_to_change_an_annotation_online(self):
        """Test to update an Annotation from revised to not revised and back to revised."""
        doc = self.prj.get_document_by_id(44864)
        annotations = doc.annotations(start_offset=10, end_offset=200)
        first_annotation = annotations[0]
        first_annotation.revised = False
        first_annotation.save()

    @classmethod
    def tearDownClass(cls) -> None:
        """Test if the Project remains the same as in the beginning."""
        assert len(cls.prj.documents) == cls.document_count
        assert len(cls.prj.test_documents) == cls.test_document_count
        category = cls.prj.get_category_by_id(63)
        assert len(cls.prj.labels[0].annotations(categories=[category])) == cls.annotations_correct


class TestKonfuzioForceOfflineData(unittest.TestCase):
    """Test handle data forced offline."""

    def test_force_offline_project(self):
        """Test that a Project with an ID can be forced offline."""
        prj = Project(id_=TEST_PROJECT_ID)
        prj.set_offline()
        self.assertFalse(prj.is_online)
        # all Data belonging to that project should be offline without setting individual instances offline
        category = Category(prj, id_=1)
        self.assertFalse(category.is_online)
        label_set = LabelSet(prj, categories=[category], id_=1)
        self.assertFalse(label_set.is_online)
        doc = Document(prj, category=category, id_=1)
        self.assertFalse(doc.is_online)
        annotation_set = AnnotationSet(doc, label_set, id_=1)
        self.assertFalse(annotation_set.is_online)
        label = Label(prj, label_set=label_set, id_=1)
        self.assertFalse(label.is_online)
        annotation = Annotation(
            doc, annotation_set=annotation_set, label_set=label_set, label=label, id_=1, spans=[Span(0, 1)]
        )
        self.assertFalse(annotation.is_online)
        prj.delete()

    def test_make_sure_annotations_are_not_downloaded_automatically(self):
        """Test that Annotations are not downloaded automatically."""
        prj = Project(id_=TEST_PROJECT_ID, project_folder='another')
        doc = prj.get_document_by_id(TEST_DOCUMENT_ID)
        doc.set_offline()
        self.assertFalse(is_file(doc.annotation_file_path, raise_exception=False))
        self.assertEqual(None, doc._annotations)
        self.assertFalse(doc.annotations())
        self.assertEqual(0, len(doc._annotations))
        self.assertFalse(is_file(doc.annotation_file_path, raise_exception=False))
        with self.assertRaises(NotImplementedError):
            doc.download_document_details()
        prj.delete()

    def test_annotations_are_loadable_for_offline_project_with_id_forced_offline(self):
        """Test that Annotations are loadable for OFFLINE_PROJECT if it's given an ID and forced offline."""
        prj = Project(id_=TEST_PROJECT_ID, project_folder=OFFLINE_PROJECT)
        doc = prj.get_document_by_id(TEST_DOCUMENT_ID)
        doc.set_offline()
        self.assertTrue(is_file(doc.annotation_file_path, raise_exception=False))
        self.assertEqual(None, doc._annotations)
        self.assertTrue(doc.annotations())
        self.assertEqual(22, len(doc._annotations))
        with self.assertRaises(NotImplementedError):
            doc.download_document_details()

    def test_make_sure_annotation_sets_are_not_downloaded_automatically(self):
        """Test that Annotation Sets are not downloaded automatically."""
        prj = Project(id_=TEST_PROJECT_ID, project_folder='another2')
        doc = prj.get_document_by_id(TEST_DOCUMENT_ID)
        doc.set_offline()
        self.assertFalse(is_file(doc.annotation_set_file_path, raise_exception=False))
        self.assertEqual(None, doc._annotation_sets)
        self.assertFalse(doc.annotation_sets())
        self.assertEqual(0, len(doc._annotation_sets))
        self.assertFalse(is_file(doc.annotation_set_file_path, raise_exception=False))
        with self.assertRaises(NotImplementedError):
            doc.download_document_details()
        prj.delete()

    def test_annotations_sets_are_loadable_for_offline_project_with_id_forced_offline(self):
        """Test that AnnotationSets are loadable for OFFLINE_PROJECT if it's given an ID and forced offline."""
        prj = Project(id_=TEST_PROJECT_ID, project_folder=OFFLINE_PROJECT)
        doc = prj.get_document_by_id(TEST_DOCUMENT_ID)
        doc.set_offline()
        self.assertTrue(is_file(doc.annotation_set_file_path, raise_exception=False))
        self.assertEqual(None, doc._annotation_sets)
        self.assertTrue(doc.annotation_sets())
        self.assertEqual(24, len(doc._annotation_sets))
        with self.assertRaises(NotImplementedError):
            doc.download_document_details()

    def test_make_sure_pages_are_not_downloaded_automatically(self):
        """Test that Pages are not downloaded automatically."""
        prj = Project(id_=TEST_PROJECT_ID, project_folder='another33')
        doc = prj.get_document_by_id(TEST_DOCUMENT_ID)
        doc.set_offline()
        self.assertFalse(is_file(doc.pages_file_path, raise_exception=False))
        self.assertEqual([], doc._pages)
        self.assertFalse(doc.pages())
        self.assertFalse(is_file(doc.pages_file_path, raise_exception=False))
        with self.assertRaises(NotImplementedError):
            doc.download_document_details()
        prj.delete()

    def test_view_annotations(self):
        """Test that Document.view_annotations() gets all the right annotations."""
        project = LocalTextProject()
        document = project.test_documents[-1]
        annotations = document.view_annotations()
        assert len(annotations) == 4
        assert sorted([ann.id_ for ann in annotations]) == [16, 18, 19, 24]


class TestFillOperation(unittest.TestCase):
    """Separate Test as we add non Labels to the Project."""

    @classmethod
    def setUpClass(cls) -> None:
        """Initialize the test: https://app.konfuzio.com/projects/46/docs/44823/bbox-annotations/."""
        cls.prj = Project(id_=None, project_folder=OFFLINE_PROJECT)
        cls.doc = cls.prj.get_document_by_id(TEST_DOCUMENT_ID)
        default_label_set = cls.prj.get_label_set_by_name('Lohnabrechnung')
        assert default_label_set.labels.__len__() == 10
        cls.annotations = cls.doc.annotations(start_offset=1498, end_offset=1590, fill=True)
        cls.sorted_spans = sorted([span for annotation in cls.annotations for span in annotation.spans])
        cls.text = '198,34\n  Erna-Muster Eiermann                         KiSt      15,83   Solz        10,89\n  '
        assert cls.doc.text[1498:1590] == cls.text

    def test_number_of_annotations(self):
        """Get Annotations for all offsets in the document."""
        self.assertEqual(len(self.annotations), 7)  # 2 single line Annotation, one multiline with two spans

    def test_number_of_spans(self):
        """Get Annotations for all offsets in the document."""
        self.assertEqual(len([span for annotation in self.annotations for span in annotation.spans]), 10)

    @unittest.skip(reason="Documents without Category cannot be processed.")
    def test_fill_doc_without_category(self):
        """Try to fill a Document without Category."""
        self.prj.get_document_by_id(44864).annotations(fill=True)

    def test_fill_full_document_with_category(self):
        """Try to fill a Document with Category."""
        # Failing because the Document already has the Annotations created by fill (from the tests setup)
        with self.assertRaises(ValueError) as context:
            self.prj.get_document_by_id(TEST_DOCUMENT_ID).annotations(fill=True)
            assert "is a duplicate of" in context.exception

    def test_correct_text_offset(self):
        """Test if the the sorted spans can create the offset text."""
        offsets = [sorted_span.offset_string for sorted_span in self.sorted_spans]
        span_text = "".join(offsets)
        self.assertEqual(self.doc.text[1498:1590], span_text)

    def test_span_start_and_end(self):
        """Test if the Spans have the correct offsets."""
        spa = [(span.start_offset, span.end_offset) for span in self.sorted_spans]
        assert self.doc.text[slice(spa[0][0], spa[0][1])] == self.doc.text[1498:1504] == '198,34'
        assert self.doc.text[slice(spa[1][0], spa[1][1])] == self.doc.text[1504:1505] == '\n'
        assert self.doc.text[slice(spa[2][0], spa[2][1])] == self.doc.text[1505:1507] == '  '
        assert self.doc.text[slice(spa[3][0], spa[3][1])] == self.doc.text[1507:1518] == 'Erna-Muster'
        assert self.doc.text[slice(spa[4][0], spa[4][1])] == self.doc.text[1518:1519] == ' '
        assert self.doc.text[slice(spa[5][0], spa[5][1])] == self.doc.text[1519:1527] == 'Eiermann'
        unlabeled = '                         KiSt      15,83   Solz        '
        assert self.doc.text[slice(spa[6][0], spa[6][1])] == self.doc.text[1527:1582] == unlabeled
        assert self.doc.text[slice(spa[7][0], spa[7][1])] == self.doc.text[1582:1587] == '10,89'
        assert self.doc.text[slice(spa[8][0], spa[8][1])] == self.doc.text[1587:1588] == '\n'
        assert self.doc.text[slice(spa[9][0], spa[9][1])] == self.doc.text[1588:1590] == '  '


class TestData(unittest.TestCase):
    """Test functions that don't require data."""

    def test_compare_none_and_id(self):
        """Test to compare an instance to None."""
        a = Data()
        a.id_ = 5
        self.assertNotEqual(a, None)

    def test_compare_nones(self):
        """Test to compare an instance with None ID to None."""
        a = Data()
        self.assertNotEqual(a, None)

    def test_compare_id_with_instance_without(self):
        """Test to compare an instance with ID to an instance with None ID."""
        a = Data()
        a.id_ = 5
        b = Data()
        self.assertNotEqual(a, b)

    def test_not_online(self):
        """Test that data with a None ID is not online."""
        a = Data()
        self.assertFalse(a.is_online)

    def test_is_online(self):
        """Test that data with an ID is online."""
        a = Data()
        a.id_ = 0
        self.assertTrue(a.is_online)

    def test_force_offline_data(self):
        """Test that data with an ID can be forced offline."""
        a = Data()
        a.id_ = 1
        a.set_offline()
        self.assertFalse(a.is_online)


def test_download_training_and_test_data():
    """Test downloading of data from training and test documents."""
    download_training_and_test_data(id_=1249)


def test_to_init_prj_from_folder():
    """Load Project from folder."""
    prj = Project(id_=46, project_folder='data_46')
    assert len(prj.documents) == 26
