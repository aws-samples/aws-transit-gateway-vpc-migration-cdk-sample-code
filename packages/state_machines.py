# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
# PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

from aws_cdk import (
    Stack,
    aws_stepfunctions as sf,
    aws_iam as iam,
    aws_dynamodb as dynamodb,
)
from constructs import Construct


class MigrationStateMachines(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.dynamodb_table()
        self.reachability_analyser()
        self.main_flow()
        self.migrate_attachment()

    # DynamoDB table to store all of the migration information
    def dynamodb_table(self):
        self.table = dynamodb.Table(
            self,
            'MigrationAutomation',
            table_name='MigrationAutomation',
            partition_key=dynamodb.Attribute(name='JobId', type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name='AttachmentId', type=dynamodb.AttributeType.STRING)
        )

    # Common X-ray permissions for all Step Function IAM Roles
    def sf_xray_permissions(self, name: str) -> iam.Role:
        role = iam.Role(
            self,
            name,
            assumed_by=iam.ServicePrincipal('states.amazonaws.com')
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    'xray:PutTraceSegments',
                    'xray:PutTelemetryRecords',
                    'xray:GetSamplingRules',
                    'xray:GetSamplingTargets'
                ],
                resources=['*'],
                effect=iam.Effect.ALLOW
            )
        )

        return role

    def reachability_analyser(self):
        role = self.sf_xray_permissions('ReachabilityAnalyserRole')

        role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name('AmazonVPCReachabilityAnalyzerFullAccessPolicy')
        )

        # Load State Machine ASL from file.
        # ASL used so that the State machine is easily editable in Step Fuctions Studio in AWS Console.
        with open('./definitions/reachability_analyser_1to1.json', 'r') as file:
            state_machine_definition = file.read()

        sf.StateMachine(
            self,
            'ReachabilityAnalyser1to1SM',
            role=role,
            state_machine_name='ReachabilityAnalyser1to1',
            state_machine_type=sf.StateMachineType.STANDARD,
            definition_body=sf.DefinitionBody.from_string(state_machine_definition)
        )

    # Main State Machine that orchestrates the whole migration
    def main_flow(self):
        role = self.sf_xray_permissions('MigrationMainFlowRole')

        # ARNs for IAM Policy statements
        sf_dependencies = ['ReachabilityAnalyser1to1', 'MigrateAttachment']
        arns = []
        for a in sf_dependencies:
            arns.append('arn:aws:states:' + self.region + ':' + self.account + ':stateMachine:' + a)

        # Allow Main State Machine to start other State Machines
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    'states:StartExecution',
                    'states:DescribeExecution',
                    'states:StopExecution',
                ],
                resources=arns,
                effect=iam.Effect.ALLOW
            )
        )

        # Other permissions required for the Main SF to work
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    'ec2:DescribeTransitGatewayVpcAttachments',
                    'dynamodb:PutItem',
                    'dynamodb:UpdateItem',
                    'ec2:CreateTags',
                    'ec2:DeleteTags',
                    'ec2:DescribeAvailabilityZones',
                    'sts:GetCallerIdentity',
                ],
                resources=['*'],
                effect=iam.Effect.ALLOW
            )
        )

        # Allow Main State Machine to write information to DynamoDB table
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    'dynamodb:PutItem',
                    'dynamodb:UpdateItem',
                ],
                resources=[self.table.table_arn],
                effect=iam.Effect.ALLOW
            )
        )

        # Allow Main SF to control events for other SFs
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    'events:PutTargets',
                    'events:PutRule',
                    'events:DescribeRule'
                ],
                resources=['*'],
                effect=iam.Effect.ALLOW
            )
        )

        # Load State Machine ASL from file.
        # ASL used so that the State machine is easily editable in Step Fuctions Studio in AWS Console.
        with open('./definitions/main_flow.json', 'r') as file:
            state_machine_definition = file.read()

        sf.StateMachine(
            self,
            'MigrateAttachmentsMainFlow',
            role=role,
            state_machine_name='MigrateAttachmentsMainFlow',
            state_machine_type=sf.StateMachineType.STANDARD,
            definition_body=sf.DefinitionBody.from_string(state_machine_definition)
        )

    # Migration State Machine
    def migrate_attachment(self):
        role = self.sf_xray_permissions('MigrateAttachmentRole')

        # IAM Permissions for the SF to do the migration
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    'ec2:GetTransitGatewayRouteTableAssociations',
                    'ec2:AssociateTransitGatewayRouteTable',
                    'ec2:DisassociateTransitGatewayRouteTable',
                    'ec2:EnableTransitGatewayRouteTablePropagation'
                ],
                resources=['*'],
                effect=iam.Effect.ALLOW
            )
        )
        
        # Load State Machine ASL from file.
        # ASL used so that the State machine is easily editable in Step Fuctions Studio in AWS Console.
        with open('./definitions/migrate_attachment.json', 'r') as file:
            state_machine_definition = file.read()

        sf.StateMachine(
            self,
            'MigrateAttachment',
            role=role,
            state_machine_name='MigrateAttachment',
            state_machine_type=sf.StateMachineType.STANDARD,
            definition_body=sf.DefinitionBody.from_string(state_machine_definition)
        )
