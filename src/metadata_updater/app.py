import json
import os
import decimal
import boto3

# Initialize DynamoDB resource
endpoint_url = os.environ.get('AWS_ENDPOINT_URL')
dynamodb = boto3.resource('dynamodb', endpoint_url=endpoint_url)

# Retrieve the table name from environment
table_name = os.environ.get('DYNAMODB_TABLE_NAME', 'ImageMetadataTable')
table = dynamodb.Table(table_name)

def handler(event, context):
    print("Received event:", json.dumps(event))
    
    if 'Records' not in event or not event['Records']:
        print("Empty event records. Nothing to update.")
        return {
            'statusCode': 200,
            'body': json.dumps('No records found in event')
        }
        
    processed_count = 0
    
    for record in event['Records']:
        message_id = record.get('messageId')
        print(f"Processing SQS message {message_id}")
        
        try:
            # Parse SQS message body
            body_str = record.get('body', '')
            body = json.loads(body_str, parse_float=decimal.Decimal)
            
            # Extract metadata
            original_key = body.get('originalKey')
            processed_key = body.get('processedKey')
            timestamp = body.get('timestamp')
            status = body.get('status')
            processing_details = body.get('processingDetails', {})
            
            if not original_key:
                raise ValueError("Message body does not contain 'originalKey'")
                
            print(f"Saving metadata to DynamoDB for key: {original_key}")
            
            # Store in DynamoDB
            table.put_item(
                Item={
                    'originalKey': original_key,
                    'processedKey': processed_key,
                    'timestamp': timestamp,
                    'status': status,
                    'processingDetails': processing_details
                }
            )
            processed_count += 1
            
        except Exception as e:
            print(f"Error processing record {message_id}: {str(e)}")
            # Re-raise the exception to trigger the SQS retry and potential DLQ redirection
            raise e
            
    return {
        'statusCode': 200,
        'body': json.dumps(f'Successfully updated {processed_count} metadata records!')
    }
