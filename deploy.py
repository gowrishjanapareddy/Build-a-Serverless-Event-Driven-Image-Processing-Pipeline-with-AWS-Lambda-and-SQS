import os
import sys
import time
import shutil
import zipfile
import subprocess
import urllib.request
import urllib.parse
import json
import boto3

# Load environment variables
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
# Set default AWS env vars if not set
os.environ['AWS_DEFAULT_REGION'] = env_vars.get('AWS_DEFAULT_REGION', 'us-east-1')
os.environ['AWS_ACCESS_KEY_ID'] = env_vars.get('AWS_ACCESS_KEY_ID', 'test')
os.environ['AWS_SECRET_ACCESS_KEY'] = env_vars.get('AWS_SECRET_ACCESS_KEY', 'test')

# Configuration
region = env_vars.get('AWS_DEFAULT_REGION', 'us-east-1')
endpoint_url = env_vars.get('LOCALSTACK_ENDPOINT', 'http://localhost:4566')
unique_id = env_vars.get('UNIQUE_ID', 'gowri-surya')
deploy_bucket_name = f"lambda-deployments-bucket-{unique_id}"
stack_name = f"image-pipeline-stack-{unique_id}"

def run_cmd(cmd, cwd=None):
    print(f"Running command: {cmd}")
    res = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error executing command: {res.stderr}")
        raise RuntimeError(res.stderr)
    return res.stdout

def zip_dir(dir_path, zip_path):
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(dir_path):
            for file in files:
                file_path = os.path.join(root, file)
                arc_path = os.path.relpath(file_path, dir_path)
                zipf.write(file_path, arc_path)

def package_lambdas():
    print("--- Packaging Lambdas ---")
    
    # 1. Image Processor Lambda
    ip_dir = os.path.abspath("src/image_processor")
    ip_dist = os.path.join(ip_dir, "dist")
    ip_zip = os.path.abspath("image_processor.zip")
    
    if os.path.exists(ip_dist):
        shutil.rmtree(ip_dist)
    os.makedirs(ip_dist)
    
    print("Installing Pillow dependencies for Linux platform...")
    # Download Pillow for target platform manylinux2014_x86_64, python version 3.12
    # This ensures it runs inside the LocalStack Linux-based Docker Lambda container
    run_cmd(
        f'pip install --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12 --target "{ip_dist}" Pillow==12.2.0'
    )
    
    # Copy app.py to dist
    shutil.copy2(os.path.join(ip_dir, "app.py"), os.path.join(ip_dist, "app.py"))
    
    # Zip the dist contents
    print(f"Creating ZIP archive at {ip_zip}...")
    if os.path.exists(ip_zip):
        os.remove(ip_zip)
    zip_dir(ip_dist, ip_zip)
    shutil.rmtree(ip_dist)
    
    # 2. Metadata Updater Lambda
    mu_dir = os.path.abspath("src/metadata_updater")
    mu_zip = os.path.abspath("metadata_updater.zip")
    
    print(f"Creating ZIP archive at {mu_zip}...")
    if os.path.exists(mu_zip):
        os.remove(mu_zip)
    with zipfile.ZipFile(mu_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
        zipf.write(os.path.join(mu_dir, "app.py"), "app.py")
        
    print("Packaging complete!")

def start_localstack():
    print("--- Starting LocalStack ---")
    run_cmd("docker-compose up -d")
    
    # Wait for health endpoint
    health_url = f"{endpoint_url}/_localstack/health"
    print(f"Waiting for LocalStack to be ready at {health_url}...")
    
    max_retries = 30
    for i in range(max_retries):
        try:
            with urllib.request.urlopen(health_url, timeout=2) as response:
                if response.status == 200:
                    health_data = json.loads(response.read().decode('utf-8'))
                    # Ensure S3, Lambda, DynamoDB, SQS are all running
                    services = health_data.get('services', {})
                    required_services = ['s3', 'lambda', 'sqs', 'dynamodb']
                    all_ready = True
                    for svc in required_services:
                        status = services.get(svc, 'unavailable')
                        if status not in ('running', 'available'):
                            all_ready = False
                            break
                    if all_ready:
                        print("LocalStack services are healthy and running!")
                        return
        except Exception:
            pass
        print(f"LocalStack not ready yet (attempt {i+1}/{max_retries}). Retrying in 2 seconds...")
        time.sleep(2)
        
    raise RuntimeError("LocalStack did not become healthy in time.")

def deploy_infrastructure():
    print("--- Deploying Infrastructure to LocalStack ---")
    
    # Configure clients with local endpoint
    s3 = boto3.client('s3', endpoint_url=endpoint_url, region_name=region)
    cf = boto3.client('cloudformation', endpoint_url=endpoint_url, region_name=region)
    
    # Create deployment S3 Bucket
    print(f"Creating deployment S3 bucket: {deploy_bucket_name}...")
    try:
        s3.create_bucket(Bucket=deploy_bucket_name)
    except s3.exceptions.BucketAlreadyExists:
        pass
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
        
    # Upload zips to deployment bucket
    print("Uploading Lambda deployment packages...")
    s3.upload_file("image_processor.zip", deploy_bucket_name, "image_processor.zip")
    s3.upload_file("metadata_updater.zip", deploy_bucket_name, "metadata_updater.zip")
    
    # Read CFN template
    with open("infra/main.yaml", "r", encoding="utf-8") as f:
        template_body = f.read()
        
    params = [
        {'ParameterKey': 'UniqueId', 'ParameterValue': unique_id},
        {'ParameterKey': 'TargetWidth', 'ParameterValue': env_vars.get('TARGET_WIDTH', '200')},
        {'ParameterKey': 'WatermarkText', 'ParameterValue': env_vars.get('WATERMARK_TEXT', '© MyCompany')},
        {'ParameterKey': 'LambdaDeploymentBucket', 'ParameterValue': deploy_bucket_name}
    ]
    
    # Delete stack if it already exists for clean deployment
    try:
        cf.describe_stacks(StackName=stack_name)
        print(f"Stack {stack_name} already exists. Deleting for fresh deployment...")
        cf.delete_stack(StackName=stack_name)
        
        while True:
            try:
                res = cf.describe_stacks(StackName=stack_name)
                status = res['Stacks'][0]['StackStatus']
                if status == 'DELETE_IN_PROGRESS':
                    print("Waiting for stack deletion to complete...")
                    time.sleep(3)
                else:
                    break
            except Exception:
                break
        print("Stack deleted successfully.")
    except Exception:
        pass
        
    print(f"Creating CloudFormation stack: {stack_name}...")
    cf.create_stack(
        StackName=stack_name,
        TemplateBody=template_body,
        Parameters=params,
        Capabilities=['CAPABILITY_NAMED_IAM']
    )
    
    # Wait for stack completion
    while True:
        res = cf.describe_stacks(StackName=stack_name)
        status = res['Stacks'][0]['StackStatus']
        print(f"Stack Status: {status}")
        if status == 'CREATE_COMPLETE':
            print("Infrastructure deployed successfully!")
            break
        elif status in ('CREATE_FAILED', 'ROLLBACK_IN_PROGRESS', 'ROLLBACK_COMPLETE'):
            reason = res['Stacks'][0].get('StackStatusReason', 'No reason specified')
            raise RuntimeError(f"Stack creation failed with status {status}. Reason: {reason}")
        time.sleep(3)

def cleanup():
    print("--- Cleaning up temporary local archives ---")
    if os.path.exists("image_processor.zip"):
        os.remove("image_processor.zip")
    if os.path.exists("metadata_updater.zip"):
        os.remove("metadata_updater.zip")
    print("Cleanup done!")

if __name__ == "__main__":
    try:
        package_lambdas()
        start_localstack()
        deploy_infrastructure()
        cleanup()
        print("\nDeployment completed successfully! Pipeline is ready to receive images.")
    except Exception as e:
        print(f"\nDeployment failed: {str(e)}", file=sys.stderr)
        sys.exit(1)
