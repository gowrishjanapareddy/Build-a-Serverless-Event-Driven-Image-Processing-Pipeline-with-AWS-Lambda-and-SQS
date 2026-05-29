import os
import sys
import time
import json
import io
import boto3
from PIL import Image
import decimal

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        return super(DecimalEncoder, self).default(obj)

# Helper to load .env variables
def load_env(env_path=".env"):
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    env_vars[key.strip()] = val.strip()
    return env_vars

env_vars = load_env()
os.environ['AWS_DEFAULT_REGION'] = env_vars.get('AWS_DEFAULT_REGION', 'us-east-1')
os.environ['AWS_ACCESS_KEY_ID'] = env_vars.get('AWS_ACCESS_KEY_ID', 'test')
os.environ['AWS_SECRET_ACCESS_KEY'] = env_vars.get('AWS_SECRET_ACCESS_KEY', 'test')

# Configurations
region = env_vars.get('AWS_DEFAULT_REGION', 'us-east-1')
endpoint_url = env_vars.get('LOCALSTACK_ENDPOINT', 'http://localhost:4566')
unique_id = env_vars.get('UNIQUE_ID', 'gowri-surya')

input_bucket_name = f"input-image-bucket-{unique_id}"
processed_bucket_name = f"processed-image-bucket-{unique_id}"
metadata_table_name = f"ImageMetadataTable-{unique_id}"
dlq_processor_name = f"DLQProcessorErrors-{unique_id}"

# Initialize Clients
s3 = boto3.client('s3', endpoint_url=endpoint_url, region_name=region)
sqs = boto3.client('sqs', endpoint_url=endpoint_url, region_name=region)
dynamodb = boto3.resource('dynamodb', endpoint_url=endpoint_url, region_name=region)
table = dynamodb.Table(metadata_table_name)

def generate_test_image():
    print("Generating 800x600 solid blue test PNG image...")
    img = Image.new('RGB', (800, 600), color='blue')
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr.getvalue()

def run_e2e_test():
    print("==================================================")
    print("          STARTING END-TO-END INTEGRATION TEST    ")
    print("==================================================")
    
    # 1. Generate test image
    test_image_data = generate_test_image()
    original_key = f"test_image_{int(time.time())}.png"
    
    # 2. Upload test image to input bucket
    print(f"Uploading image to s3://{input_bucket_name}/{original_key}...")
    s3.put_object(Bucket=input_bucket_name, Key=original_key, Body=test_image_data)
    
    # 3. Poll processed bucket for resized image
    print("Polling processed S3 bucket for resized image...")
    processed_key = f"resized_{original_key}"
    found_s3 = False
    timeout = 30
    start = time.time()
    
    while time.time() - start < timeout:
        try:
            res = s3.head_object(Bucket=processed_bucket_name, Key=processed_key)
            found_s3 = True
            print(f"SUCCESS: Processed image found! Size: {res['ContentLength']} bytes")
            break
        except Exception:
            time.sleep(1)
            
    if not found_s3:
        print("ERROR: Resized image was not found in processed S3 bucket.", file=sys.stderr)
        sys.exit(1)
        
    # Download processed image and verify properties
    local_output = "temp_output_verify.png"
    s3.download_file(processed_bucket_name, processed_key, local_output)
    try:
        with Image.open(local_output) as img:
            print(f"SUCCESS: Downloaded processed image has dimensions: {img.size[0]}x{img.size[1]}")
            # Target width default is 200, so verify that
            expected_width = int(env_vars.get('TARGET_WIDTH', '200'))
            if img.size[0] != expected_width:
                print(f"WARNING: Image width {img.size[0]} does not match expected TARGET_WIDTH {expected_width}", file=sys.stderr)
    finally:
        if os.path.exists(local_output):
            os.remove(local_output)
            
    # 4. Poll DynamoDB for metadata entry
    print("Polling DynamoDB ImageMetadataTable for record...")
    found_db = False
    start = time.time()
    
    while time.time() - start < timeout:
        res = table.get_item(Key={'originalKey': original_key})
        if 'Item' in res:
            found_db = True
            item = res['Item']
            print("SUCCESS: Metadata entry found in DynamoDB:")
            print(json.dumps(item, indent=2, cls=DecimalEncoder))
            
            # Assert schema values
            assert item['originalKey'] == original_key
            assert item['processedKey'] == processed_key
            assert item['status'] == 'SUCCESS'
            assert 'processingDetails' in item
            print("SUCCESS: DynamoDB item schema is fully correct!")
            break
        time.sleep(1)
        
    if not found_db:
        print("ERROR: Metadata record not found in DynamoDB.", file=sys.stderr)
        sys.exit(1)
        
    # 5. Test error DLQ path with invalid text file upload
    print("--------------------------------------------------")
    print("Testing error flow with invalid file type...")
    invalid_key = f"invalid_file_{int(time.time())}.txt"
    s3.put_object(Bucket=input_bucket_name, Key=invalid_key, Body=b"This is plain text and not an image.")
    
    # Get DLQ SQS URL
    try:
        queue_url_res = sqs.get_queue_url(QueueName=dlq_processor_name)
        dlq_url = queue_url_res['QueueUrl']
    except Exception as e:
        print(f"ERROR: Could not fetch URL for {dlq_processor_name}: {str(e)}", file=sys.stderr)
        sys.exit(1)
        
    print(f"Polling DLQ {dlq_processor_name} for error record...")
    found_dlq = False
    start = time.time()
    
    while time.time() - start < timeout:
        res = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=1, WaitTimeSeconds=1)
        if 'Messages' in res:
            msg = res['Messages'][0]
            body = json.loads(msg['Body'])
            print("SUCCESS: Received expected error message in DLQProcessorErrors:")
            print(json.dumps(body, indent=2))
            
            # Assert schema values
            assert body['originalKey'] == invalid_key
            assert body['errorType'] == 'ValueError'
            assert 'Unsupported file format' in body['errorMessage']
            
            # Clean up the message from DLQ
            sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=msg['ReceiptHandle'])
            found_dlq = True
            break
        time.sleep(1)
        
    if not found_dlq:
        print("ERROR: Error message did not arrive in SQS DLQProcessorErrors.", file=sys.stderr)
        sys.exit(1)
        
    print("==================================================")
    print("      ALL END-TO-END TESTS PASSED SUCCESSFULLY!   ")
    print("==================================================")

if __name__ == "__main__":
    try:
        run_e2e_test()
    except Exception as e:
        print(f"E2E Test crashed: {str(e)}", file=sys.stderr)
        sys.exit(1)
