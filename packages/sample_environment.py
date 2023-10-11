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
    Stack, CfnTag, CfnOutput,
    aws_ec2 as ec2,
    aws_iam as iam
    )
from constructs import Construct


class SampleEnvironment(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Transit Gateway and Route Tables
        tgw = ec2.CfnTransitGateway(
            self,
            "TGW",
            auto_accept_shared_attachments="enable",
            tags=[
                CfnTag(
                    key='Name',
                    value='MigrationTGW'
                )
            ]
        )

        CfnOutput(
            self,
            'TransitGatewayIdOutput',
            value=tgw.attr_id,
            export_name='MigrationTransitGatewayID'
        )

        # Enable default propagation and association to main TGW route table.
        tgw.default_route_table_association = 'enable'
        tgw.default_route_table_propagation = 'enable'

        # Target TGW Route Table that is used as a target for the migrations
        target_route_table = ec2.CfnTransitGatewayRouteTable(
            self,
            "TargetRT",
            transit_gateway_id=tgw.ref,
            tags=[
                CfnTag(
                    key='Name',
                    value='TargetRT'
                )
            ]
        )
        CfnOutput(
            self,
            'TargetRouteTableID',
            value=target_route_table.attr_id,
            export_name='TargetRouteTableID'
        )

        # IAM Role for the EC2 Instances so that those can connect to Systems Manager for remote connections.
        ssm_role = iam.Role(
            self,
            "SSMRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")
            ],
        )

        # List of VPCs to be created. One VPC is used for communication validation and two are to be migrated.
        vpcs = [
            {
                'vpc_name': 'validation_vpc',
                'cidr': '10.0.0.0/16'
            },
            {
                'vpc_name': 'migrate_vpc_a',
                'cidr': '10.1.0.0/16'
            },
            {
                'vpc_name': 'migrate_vpc_b',
                'cidr': '10.2.0.0/16'
            }
        ]

        for vpcinfo in vpcs:
            vpc = ec2.Vpc(
                self,
                vpcinfo['vpc_name'],
                ip_addresses=ec2.IpAddresses.cidr(vpcinfo['cidr']),
                vpc_name=vpcinfo['vpc_name'],
                max_azs=2,
                nat_gateways=0,
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name='private',
                        subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                        cidr_mask=26
                    ),
                    ec2.SubnetConfiguration(
                        name='tgw',
                        subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                        cidr_mask=26
                    )
                ]
            )

            attachment = ec2.CfnTransitGatewayAttachment(
                self,
                vpcinfo['vpc_name'] + 'Attachment',
                transit_gateway_id=tgw.ref,
                vpc_id=vpc.vpc_id,
                subnet_ids=vpc.select_subnets(subnet_group_name='tgw').subnet_ids,
                tags=[
                    CfnTag(
                        key='Name',
                        value=vpcinfo['vpc_name']
                    )
                ]
            )

            CfnOutput(
                self,
                f'{vpcinfo["vpc_name"]}-attachmentID',
                value=attachment.attr_id,
                export_name=f'{vpcinfo["vpc_name"].replace("_", "")}AttachmentID'
            )

            private_subnets = vpc.select_subnets(subnet_group_name="private")

            for subnet in private_subnets.subnets:
                subnet_name = subnet.node.path.split("/")[-1]
                ec2.CfnRoute(
                    self,
                    f'{vpcinfo["vpc_name"]}-tgw-route-{subnet_name}',
                    destination_cidr_block='0.0.0.0/0',
                    transit_gateway_id=tgw.ref,
                    route_table_id=subnet.route_table.route_table_id,
                ).node.add_dependency(attachment)
                
            vpc.add_interface_endpoint(
                f'{vpcinfo["vpc_name"]}-SSM',
                service=ec2.InterfaceVpcEndpointAwsService.SSM,
                subnets=ec2.SubnetSelection(subnet_group_name='tgw')
            )
            vpc.add_interface_endpoint(
                f'{vpcinfo["vpc_name"]}-SSMMESSAGES',
                service=ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES,
                subnets=ec2.SubnetSelection(subnet_group_name='tgw')
            )
            vpc.add_interface_endpoint(
                f'{vpcinfo["vpc_name"]}-EC2MESSAGES',
                service=ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES,
                subnets=ec2.SubnetSelection(subnet_group_name='tgw')
            )

            security_group = ec2.SecurityGroup(
                self,
                f'{vpcinfo["vpc_name"]}-WorkloadEC2SG',
                security_group_name="workload-sg",
                vpc=vpc
            )

            # Allow ports 80 and 443 for reachability analyser tests.
            security_group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80))
            security_group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443))

            # EC2 instance for connectivity tests.
            ec2.Instance(
                self,
                f'{vpcinfo["vpc_name"]}-WorkloadEC2',
                vpc=vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_group_name='private'),
                instance_type=ec2.InstanceType.of(
                    ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.MICRO
                ),
                machine_image=ec2.MachineImage.latest_amazon_linux2023(
                    cpu_type=ec2.AmazonLinuxCpuType.ARM_64,
                ),
                role=ssm_role,
                security_group=security_group,
            )