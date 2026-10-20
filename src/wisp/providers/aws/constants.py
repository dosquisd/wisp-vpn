DEFAULT_AWS_REGION: str = "us-east-2"
DEFAULT_EC2_INSTANCE_TYPE: str = "t3.micro"

# AMI related. Actually, this depends on the region, we need to ensure that this AMI is, at least,
# available in the region we are deploying to. For now, we will use a default AMI name and owner.
DEFAULT_AMI_NAME: str = (
    "ubuntu/images/hvm-ssd-gp3/ubuntu-resolute-26.04-amd64-server-20260619"
)
DEFAULT_AMI_OWNER: str = "099720109477"  # Canonical
