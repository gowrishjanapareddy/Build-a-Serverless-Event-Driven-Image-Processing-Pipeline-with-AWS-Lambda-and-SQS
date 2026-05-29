import os
import json
import unittest
from unittest.mock import patch, MagicMock, mock_open
import sys

# Mock boto3 at module import time
mock_boto3 = MagicMock()
sys.modules['boto3'] = mock_boto3

# Mock PIL to prevent attempts to load real library or images in mock tests
mock_pil = MagicMock()
sys.modules['PIL'] = mock_pil
sys.modules['PIL.Image'] = mock_pil.Image
sys.modules['PIL.ImageDraw'] = mock_pil.ImageDraw
sys.modules['PIL.ImageFont'] = mock_pil.ImageFont

# Now we can import the app under test
import src.image_processor.app as app

class TestImageProcessor(unittest.TestCase):
    def setUp(self):
        # Reset mocks
        mock_boto3.reset_mock()
        mock_pil.reset_mock()
        
        # Configure environment variables
        os.environ['TARGET_WIDTH'] = '200'
        os.environ['WATERMARK_TEXT'] = '© MyCompany'
        os.environ['SQS_QUEUE_URL'] = 'https://sqs.us-east-1.amazonaws.com/123456789012/ImageProcessedQueue'
        os.environ['DLQ_QUEUE_URL'] = 'https://sqs.us-east-1.amazonaws.com/123456789012/DLQProcessorErrors'
        os.environ['PROCESSED_BUCKET_NAME'] = 'processed-image-bucket-test'
        
        # Set up S3/SQS client mocks
        self.s3_mock = MagicMock()
        self.sqs_mock = MagicMock()
        app.s3_client = self.s3_mock
        app.sqs_client = self.sqs_mock
        
        # S3 Test Event
        self.s3_event = {
            'Records': [
                {
                    's3': {
                        'bucket': {
                            'name': 'input-image-bucket-test'
                        },
                        'object': {
                            'key': 'photos/holiday.jpg'
                        }
                    }
                }
            ]
        }

    @patch('os.path.exists', return_value=True)
    @patch('os.remove')
    @patch('src.image_processor.app.Image.open')
    def test_handler_success(self, mock_image_open, mock_remove, mock_exists):
        # Arrange
        mock_img = MagicMock()
        mock_img.size = (1000, 500)  # Width, Height
        mock_img.mode = 'RGB'
        
        mock_resized = MagicMock()
        mock_img.resize.return_value = mock_resized
        mock_image_open.return_value = mock_img
        
        mock_draw = MagicMock()
        mock_draw.textbbox.return_value = (0, 0, 80, 15)  # Text size
        app.ImageDraw.Draw.return_value = mock_draw
        
        # Act
        response = app.handler(self.s3_event, None)
        
        # Assert
        self.assertEqual(response['statusCode'], 200)
        
        # Verify download & upload
        self.s3_mock.download_file.assert_called_once()
        self.s3_mock.upload_file.assert_called_once_with(
            unittest.mock.ANY, 
            'processed-image-bucket-test', 
            'resized_photos/holiday.jpg'
        )
        
        # Verify resizing preserving aspect ratio (200 target width, ratio = 200/1000 = 0.2, height = 500 * 0.2 = 100)
        mock_img.resize.assert_called_once()
        args, kwargs = mock_img.resize.call_args
        self.assertEqual(args[0], (200, 100))
        
        # Verify success message published to SQS
        self.sqs_mock.send_message.assert_called_once()
        call_kwargs = self.sqs_mock.send_message.call_args[1]
        self.assertEqual(call_kwargs['QueueUrl'], os.environ['SQS_QUEUE_URL'])
        
        body = json.loads(call_kwargs['MessageBody'])
        self.assertEqual(body['originalKey'], 'photos/holiday.jpg')
        self.assertEqual(body['processedKey'], 'resized_photos/holiday.jpg')
        self.assertEqual(body['status'], 'SUCCESS')
        self.assertEqual(body['processingDetails']['originalSize'], '1000x500')
        self.assertEqual(body['processingDetails']['newSize'], '200x100')

    @patch('src.image_processor.app.Image.open')
    def test_handler_invalid_file_extension(self, mock_image_open):
        # Arrange
        invalid_event = {
            'Records': [
                {
                    's3': {
                        'bucket': {'name': 'input-image-bucket-test'},
                        'object': {'key': 'document.txt'}
                    }
                }
            ]
        }
        
        # Act & Assert
        with self.assertRaises(ValueError):
            app.handler(invalid_event, None)
            
        # Verify error message published to DLQ
        self.sqs_mock.send_message.assert_called_once()
        call_kwargs = self.sqs_mock.send_message.call_args[1]
        self.assertEqual(call_kwargs['QueueUrl'], os.environ['DLQ_QUEUE_URL'])
        
        body = json.loads(call_kwargs['MessageBody'])
        self.assertEqual(body['originalKey'], 'document.txt')
        self.assertEqual(body['errorType'], 'ValueError')
        self.assertIn('Unsupported file format', body['errorMessage'])

if __name__ == '__main__':
    unittest.main()
