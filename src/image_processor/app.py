import json
import os
import time
import urllib.parse
from datetime import datetime
import boto3
from PIL import Image, ImageDraw, ImageFont

# Initialize boto3 clients
endpoint_url = os.environ.get('AWS_ENDPOINT_URL')
s3_client = boto3.client('s3', endpoint_url=endpoint_url)
sqs_client = boto3.client('sqs', endpoint_url=endpoint_url)

def handler(event, context):
    print("Received event:", json.dumps(event))
    
    # Check if this is an S3 test event or actual event
    if 'Records' not in event or not event['Records']:
        print("Empty event records. Nothing to process.")
        return {
            'statusCode': 200,
            'body': json.dumps('No records found in event')
        }
        
    record = event['Records'][0]
    
    # Support both direct S3 invoke and other shapes if any
    if 's3' not in record:
        print("Record is not an S3 notification. Skipping.")
        return {
            'statusCode': 200,
            'body': json.dumps('Not an S3 event record')
        }
        
    bucket_name = record['s3']['bucket']['name']
    original_key = urllib.parse.unquote_plus(record['s3']['object']['key'])
    
    print(f"Triggered for bucket: {bucket_name}, key: {original_key}")
    
    start_time = time.time()
    temp_input_path = f"/tmp/input_{int(start_time)}"
    temp_output_path = f"/tmp/output_{int(start_time)}"
    
    try:
        # Step 1: Validate file type by extension
        ext = os.path.splitext(original_key)[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png'):
            raise ValueError(f"Unsupported file format '{ext}'. Only JPG, JPEG, and PNG are allowed.")
            
        # Step 2: Download image
        print(f"Downloading s3://{bucket_name}/{original_key} to {temp_input_path}")
        s3_client.download_file(bucket_name, original_key, temp_input_path)
        
        # Step 3: Validate and process image
        print("Processing image...")
        img = Image.open(temp_input_path)
        
        # Keep track of original details
        original_size_str = f"{img.size[0]}x{img.size[1]}"
        original_width, original_height = img.size
        
        # Convert mode to RGB or RGBA for processing
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGB')
            
        # Resize image keeping aspect ratio
        target_width = int(os.environ.get('TARGET_WIDTH', '200'))
        ratio = target_width / original_width
        target_height = int(original_height * ratio)
        
        print(f"Resizing from {original_size_str} to {target_width}x{target_height}")
        resized_img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
        
        # Apply watermark
        watermark_text = os.environ.get('WATERMARK_TEXT', '© MyCompany')
        draw = ImageDraw.Draw(resized_img)
        font = ImageFont.load_default()
        
        # Use textbbox to get size in newer Pillow versions
        bbox = draw.textbbox((0, 0), watermark_text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        
        # Place at bottom-right corner with 10px margin
        margin = 10
        x = target_width - text_width - margin
        y = target_height - text_height - margin
        
        # Ensure coordinates are within image bounds
        x = max(0, x)
        y = max(0, y)
        
        # Draw shadow (black) and text (white)
        draw.text((x + 1, y + 1), watermark_text, fill="black", font=font)
        draw.text((x, y), watermark_text, fill="white", font=font)
        
        # Save processed image to disk
        # Save format depends on extension
        save_format = 'PNG' if ext == '.png' else 'JPEG'
        resized_img.save(temp_output_path, format=save_format)
        
        # Step 4: Upload processed image
        processed_bucket = os.environ.get('PROCESSED_BUCKET_NAME')
        if not processed_bucket:
            raise ValueError("Environment variable PROCESSED_BUCKET_NAME is not set.")
            
        processed_key = f"resized_{original_key}"
        print(f"Uploading processed image to s3://{processed_bucket}/{processed_key}")
        s3_client.upload_file(temp_output_path, processed_bucket, processed_key)
        
        # Step 5: Publish success message to SQS
        duration_ms = (time.time() - start_time) * 1000
        sqs_queue_url = os.environ.get('SQS_QUEUE_URL')
        if not sqs_queue_url:
            raise ValueError("Environment variable SQS_QUEUE_URL is not set.")
            
        success_payload = {
            "originalKey": original_key,
            "processedKey": processed_key,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "status": "SUCCESS",
            "processingDetails": {
                "originalSize": original_size_str,
                "newSize": f"{target_width}x{target_height}",
                "durationMs": round(duration_ms, 2)
            }
        }
        
        print(f"Publishing success message to SQS: {sqs_queue_url}")
        sqs_client.send_message(
            QueueUrl=sqs_queue_url,
            MessageBody=json.dumps(success_payload)
        )
        
        return {
            'statusCode': 200,
            'body': json.dumps('Image processing completed successfully!')
        }
        
    except Exception as e:
        print(f"Error processing image {original_key}: {str(e)}")
        
        # Publish error message to DLQProcessorErrors
        dlq_queue_url = os.environ.get('DLQ_QUEUE_URL')
        if dlq_queue_url:
            try:
                error_payload = {
                    "originalKey": original_key if 'original_key' in locals() else "unknown",
                    "errorType": type(e).__name__,
                    "errorMessage": str(e),
                    "timestamp": datetime.utcnow().isoformat() + "Z"
                }
                print(f"Publishing error message to DLQ: {dlq_queue_url}")
                sqs_client.send_message(
                    QueueUrl=dlq_queue_url,
                    MessageBody=json.dumps(error_payload)
                )
            except Exception as sqs_err:
                print(f"Failed to send error message to SQS DLQ: {str(sqs_err)}")
        else:
            print("DLQ_QUEUE_URL not configured. Skipping error SQS message.")
            
        # Re-raise the exception so Lambda itself is aware of the failure
        raise e
        
    finally:
        # Cleanup temporary files
        if os.path.exists(temp_input_path):
            try:
                os.remove(temp_input_path)
            except OSError:
                pass
        if os.path.exists(temp_output_path):
            try:
                os.remove(temp_output_path)
            except OSError:
                pass
