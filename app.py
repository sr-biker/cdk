import aws_cdk as cdk
from stacks.rds_stack import RdsStack
from stacks.atlantis_stack import AtlantisStack

app = cdk.App()

env = cdk.Environment(account="605448157849", region="us-east-2")

RdsStack(app, "RdsStack", env=env)
AtlantisStack(app, "AtlantisStack", env=env)

app.synth()
