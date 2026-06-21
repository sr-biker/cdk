from aws_cdk import (
    Stack,
    RemovalPolicy,
    CfnOutput,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
    aws_scheduler as scheduler,
)
from constructs import Construct


class AtlantisStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # VPC with a single public subnet — Atlantis needs to receive GitHub webhooks
        vpc = ec2.Vpc(
            self,
            "AtlantisVpc",
            max_azs=1,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ],
        )

        # Security group: allow GitHub webhook IPs on 4141 and SSH for debugging
        sg = ec2.SecurityGroup(
            self,
            "AtlantisSg",
            vpc=vpc,
            description="Atlantis server security group",
        )
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(4141), "Atlantis webhook port")
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(22), "SSH")

        # Secret holding GitHub credentials — populate values manually after deploy
        gh_secret = secretsmanager.Secret(
            self,
            "AtlantisGhSecret",
            secret_name="/atlantis/github",
            description="GitHub token and webhook secret for Atlantis",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"gh_user":"REPLACE_ME","gh_token":"REPLACE_ME","webhook_secret":"REPLACE_ME","repo_allowlist":"REPLACE_ME"}',
                generate_string_key="dummy",
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # IAM role — CDK deploy needs broad permissions; scope down for production
        role = iam.Role(
            self,
            "AtlantisRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess"),
            ],
        )

        # Allow the instance to read the GitHub secret
        gh_secret.grant_read(role)

        # User data: install Docker, build custom Atlantis image, run it
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            # System setup
            "yum update -y",
            "yum install -y docker git jq aws-cli",
            "systemctl enable docker && systemctl start docker",

            # Fetch GitHub credentials from Secrets Manager
            f"SECRET=$(aws secretsmanager get-secret-value --secret-id {gh_secret.secret_name} --region {self.region} --query SecretString --output text)",
            'GH_USER=$(echo $SECRET | jq -r .gh_user)',
            'GH_TOKEN=$(echo $SECRET | jq -r .gh_token)',
            'WEBHOOK_SECRET=$(echo $SECRET | jq -r .webhook_secret)',
            'REPO_ALLOWLIST=$(echo $SECRET | jq -r .repo_allowlist)',

            # Build custom Atlantis image with Python + AWS CDK
            "mkdir -p /atlantis-build",
            "cat > /atlantis-build/Dockerfile << 'DOCKERFILE'",
            "FROM ghcr.io/runatlantis/atlantis:latest",
            "USER root",
            "RUN apk add --no-cache python3 py3-pip nodejs npm && \\",
            "    npm install -g aws-cdk && \\",
            "    ln -sf python3 /usr/bin/python",
            "USER atlantis",
            "DOCKERFILE",
            "docker build -t atlantis-cdk /atlantis-build/",

            # Run Atlantis
            "docker run -d --restart unless-stopped \\",
            "  --name atlantis \\",
            "  -p 4141:4141 \\",
            '  -e ATLANTIS_GH_USER="$GH_USER" \\',
            '  -e ATLANTIS_GH_TOKEN="$GH_TOKEN" \\',
            '  -e ATLANTIS_GH_WEBHOOK_SECRET="$WEBHOOK_SECRET" \\',
            '  -e ATLANTIS_REPO_ALLOWLIST="$REPO_ALLOWLIST" \\',
            "  -e ATLANTIS_PORT=4141 \\",
            "  -e ATLANTIS_REPO_CONFIG=/etc/atlantis/repos.yaml \\",
            "  atlantis-cdk server",
        )

        # EC2 instance — t3.small gives enough memory to run Docker + CDK synth
        instance = ec2.Instance(
            self,
            "AtlantisInstance",
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
            machine_image=ec2.AmazonLinuxImage(
                generation=ec2.AmazonLinuxGeneration.AMAZON_LINUX_2023,
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=sg,
            role=role,
            user_data=user_data,
            associate_public_ip_address=True,
        )

        # Elastic IP so the webhook URL stays stable across reboots
        eip = ec2.CfnEIP(self, "AtlantisEip", instance_id=instance.instance_id)

        # IAM role for EventBridge Scheduler to stop/start the EC2 instance
        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            inline_policies={
                "StopStartEc2": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["ec2:StopInstances", "ec2:StartInstances"],
                            resources=[f"arn:aws:ec2:{self.region}:{self.account}:instance/{instance.instance_id}"],
                        )
                    ]
                )
            },
        )

        # Stop at 6 PM ET (23:00 UTC) on weekdays
        scheduler.CfnSchedule(
            self,
            "StopAtlantis",
            schedule_expression="cron(0 23 ? * MON-FRI *)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn="arn:aws:scheduler:::aws-sdk:ec2:stopInstances",
                role_arn=scheduler_role.role_arn,
                input=f'{{"InstanceIds":["{instance.instance_id}"]}}',
            ),
        )

        # Start at 8 AM ET (13:00 UTC) on weekdays
        scheduler.CfnSchedule(
            self,
            "StartAtlantis",
            schedule_expression="cron(0 13 ? * MON-FRI *)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn="arn:aws:scheduler:::aws-sdk:ec2:startInstances",
                role_arn=scheduler_role.role_arn,
                input=f'{{"InstanceIds":["{instance.instance_id}"]}}',
            ),
        )

        CfnOutput(self, "AtlantisUrl", value=f"http://{eip.ref}:4141")
        CfnOutput(self, "WebhookUrl", value=f"http://{eip.ref}:4141/events")
        CfnOutput(self, "SecretArn", value=gh_secret.secret_arn)
        CfnOutput(self, "InstanceId", value=instance.instance_id)
