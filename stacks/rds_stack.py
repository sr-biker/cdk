from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_rds as rds,
    aws_scheduler as scheduler,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class RdsStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # VPC — use existing default or create a new one
        vpc = ec2.Vpc(
            self,
            "RdsVpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Isolated",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=28,
                ),
            ],
        )

        # Security group for the RDS instance
        db_security_group = ec2.SecurityGroup(
            self,
            "DbSecurityGroup",
            vpc=vpc,
            description="Security group for RDS PostgreSQL instance",
            allow_all_outbound=False,
        )

        # Allow inbound PostgreSQL traffic from within the VPC
        db_security_group.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(5432),
            description="Allow PostgreSQL from within VPC",
        )

        # Subnet group — place the DB in isolated subnets
        subnet_group = rds.SubnetGroup(
            self,
            "DbSubnetGroup",
            description="Subnet group for RDS instance",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
        )

        # Credentials stored in Secrets Manager
        db_credentials = rds.Credentials.from_generated_secret(
            username="dbadmin",
            secret_name="/rds/postgres/credentials",
        )

        # RDS PostgreSQL instance
        self.db_instance = rds.DatabaseInstance(
            self,
            "PostgresInstance",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_17
            ),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.T3, ec2.InstanceSize.MICRO
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            subnet_group=subnet_group,
            security_groups=[db_security_group],
            credentials=db_credentials,
            database_name="appdb",
            multi_az=False,
            allocated_storage=20,
            max_allocated_storage=100,
            storage_encrypted=True,
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
            publicly_accessible=False,
            enable_performance_insights=False,
            monitoring_interval=Duration.seconds(0),
        )

        db_arn = f"arn:aws:rds:{self.region}:{self.account}:db:{self.db_instance.instance_identifier}"

        scheduler_role = iam.Role(
            self,
            "RdsSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            inline_policies={
                "StopStartRds": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["rds:StopDBInstance", "rds:StartDBInstance"],
                            resources=[db_arn],
                        )
                    ]
                )
            },
        )

        # Stop at 11 PM ET (04:00 UTC) on weekdays
        scheduler.CfnSchedule(
            self,
            "StopRds",
            schedule_expression="cron(0 4 ? * TUE-SAT *)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn="arn:aws:scheduler:::aws-sdk:rds:stopDBInstance",
                role_arn=scheduler_role.role_arn,
                input=f'{{"DbInstanceIdentifier":"{self.db_instance.instance_identifier}"}}',
            ),
        )

        # Start at 8 AM ET (13:00 UTC) on weekdays
        scheduler.CfnSchedule(
            self,
            "StartRds",
            schedule_expression="cron(0 13 ? * MON-FRI *)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn="arn:aws:scheduler:::aws-sdk:rds:startDBInstance",
                role_arn=scheduler_role.role_arn,
                input=f'{{"DbInstanceIdentifier":"{self.db_instance.instance_identifier}"}}',
            ),
        )
