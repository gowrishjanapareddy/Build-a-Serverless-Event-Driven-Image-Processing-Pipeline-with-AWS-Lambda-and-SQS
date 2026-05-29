import os
import json
import unittest
from unittest.mock import MagicMock
import sys

# Mock boto3 at module import time
mock_boto3 = MagicMock()
sys.modules['boto3'] = mock_boto3

# Import app under test
import src.metadata_updater.app as app

class TestMetadataUpdater(unittest.TestCase):
    def setUp(self):
        mock_boto3.reset_mock()
        
        # Configure env variables
        os.environ['DYNAMODB_TABLE_NAME'] = 'ImageMetadataTable-test'
        
        # Mock DynamoDB Table
        self.table_mock = MagicMock()
        app.table = self.table_mock
        
        # SQS Test Event
        self.sqs_payload = {
            "originalKey": "photos/beach.png",
            "processedKey": "resized_photos/beach.png",
            "timestamp": "2026-05-29T15:00:00Z",
            "status": "SUCCESS",
            "processingDetails": {
                "originalSize": "1920x1080",
                "newSize": "200x112",
                "durationMs": 85.5
            }
        }
        
        self.sqs_event = {
            'Records': [
                {
                    'messageId': 'msg-12345',
                    'body': json.dumps(self.sqs_payload)
                }
            ]
        }

    def test_handler_success(self):
        # Act
        response = app.handler(self.sqs_event, None)
        
        # Assert
        self.assertEqual(response['statusCode'], 200)
        self.assertIn('Successfully updated 1 metadata records!', response['body'])
        
        # Verify DynamoDB put_item was called with correct structure
        self.table_mock.put_item.assert_called_once_with(
            Item={
                'originalKey': 'photos/beach.png',
                'processedKey': 'resized_photos/beach.png',
                'timestamp': '2026-05-29T15:00:00Z',
                'status': 'SUCCESS',
                'processingDetails': {
                    'originalSize': '1920x1080',
                    'newSize': '200x112',
                    'durationMs': 85.5
                }
            }
        )

    def test_handler_missing_original_key(self):
        # Arrange - Payload missing originalKey
        bad_payload = self.sqs_payload.copy()
        del bad_payload['originalKey']
        bad_event = {
            'Records': [
                {
                    'messageId': 'msg-bad',
                    'body': json.dumps(bad_payload)
                }
            ]
        }
        
        # Act & Assert
        with self.assertRaises(ValueError):
            app.handler(bad_event, None)
            
        # Verify DynamoDB was NOT called
        self.table_mock.put_item.assert_not_called()

    def test_handler_dynamodb_failure(self):
        # Arrange - make put_item raise exception
        self.table_mock.put_item.side_effect = RuntimeError("DynamoDB connection lost")
        
        # Act & Assert
        with self.assertRaises(RuntimeError):
            app.handler(self.sqs_event, None)
            
        # Verify put_item was called
        self.table_mock.put_item.assert_called_once()

if __name__ == '__main__':
    unittest.main()
