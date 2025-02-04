"""Test the data on which other Tests rely on."""
import unittest

from konfuzio_sdk.samples import LocalTextProject


class TestLocalTextProject(unittest.TestCase):
    """Test the data on which other Tests rely on."""

    def test_number_of_training_documents(self):
        """Test the number of all training Documents."""
        project = LocalTextProject()
        assert len(project.documents) == 2

    def test_number_of_test_documents(self):
        """Test the number of all test Documents."""
        project = LocalTextProject()
        assert len(project.test_documents) == 4

    def test_number_of_categories(self):
        """Test the number of all Categories."""
        project = LocalTextProject()
        assert len(project.categories) == 2

    def test_number_of_labels(self):
        """Test the number of all labels."""
        project = LocalTextProject()
        assert len(project.labels) == 10
