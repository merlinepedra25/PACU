#!/usr/bin/env python3
import argparse
from copy import deepcopy
import time


module_info = {
    # Name of the module (should be the same as the filename)
    'name': 'enum_elb_logging',

    # Name and any other notes about the author
    'author': 'Spencer Gietzen of Rhino Security Labs',

    # Category of the module. Make sure the name matches an existing category.
    'category': 'recon_enum_with_keys',

    # One liner description of the module functionality. This shows up when a user searches for modules.
    'one_liner': 'Collects a list of Elastic Load Balancers without access logging.',

    # Description about what the module does and how it works
    'description': 'This module will enumerate all EC2 Elastic Load Balancers and save their data to the current session, as well as write a list of ELBs with logging disabled to ./sessions/[current_session_name]/downloads/elbs_no_logs_[timestamp].csv.',

    # A list of AWS services that the module utilizes during its execution
    'services': ['ElasticLoadBalancing'],

    # For prerequisite modules, try and see if any existing modules return the data that is required for your module before writing that code yourself, that way, session data can stay separated and modular.
    'prerequisite_modules': [],

    # Module arguments to autocomplete when the user hits tab
    'arguments_to_autocomplete': ['--regions'],
}

parser = argparse.ArgumentParser(add_help=False, description=module_info['description'])

parser.add_argument('--regions', required=False, default=None, help='One or more (comma separated) AWS regions in the format "us-east-1". Defaults to all session regions.')


def help():
    return [module_info, parser.format_help()]


def main(args, pacu_main):
    session = pacu_main.get_active_session()

    args = parser.parse_args(args)
    print = pacu_main.print
    get_regions = pacu_main.get_regions

    regions = get_regions('elasticloadbalancing')

    if 'LoadBalancers' not in session.EC2.keys():
        ec2_data = deepcopy(session.EC2)
        ec2_data['LoadBalancers'] = []
        session.update(pacu_main.database, EC2=ec2_data)

    load_balancers = list()
    for region in regions:
        print(f'Starting region {region}...')
        client = pacu_main.get_boto3_client('elbv2', region)

        count = 0
        response = None
        next_marker = False

        while (response is None or 'NextMarker' in response):
            if next_marker is False:
                response = client.describe_load_balancers()
            else:
                response = client.describe_load_balancers(Marker=next_marker)

            if 'NextMarker' in response:
                next_marker = response['NextMarker']
            for load_balancer in response['LoadBalancers']:
                load_balancer['Region'] = region
                # Adding Attributes to current load balancer database
                load_balancer['Attributes'] = client.describe_load_balancer_attributes(
                    LoadBalancerArn=load_balancer['LoadBalancerArn']
                )['Attributes']
                load_balancers.append(load_balancer)

            count += len(response['LoadBalancers'])

        print(f'  {count} total load balancer(s) found in {region}.')

    ec2_data = deepcopy(session.EC2)
    ec2_data['LoadBalancers'] = deepcopy(load_balancers)
    session.update(pacu_main.database, EC2=ec2_data)

    print(f"{len(session.EC2['LoadBalancers'])} total load balancer(s) found.")

    now = time.time()
    csv_file_path = f'sessions/{session.name}/downloads/elbs_no_logs_{now}.csv'

    with open(csv_file_path, 'w+') as csv_file:
        csv_file.write('Load Balancer Name,Load Balancer ARN,Region\n')
        for load_balancer in session.EC2['LoadBalancers']:
            print(load_balancer)
            for attribute in load_balancer['Attributes']:
                if attribute['Key'] == 'access_logs.s3.enabled':
                    if attribute['Value'] is False or attribute['Value'] == 'false':
                        csv_file.write(f"{load_balancer['LoadBalancerName']},{load_balancer['LoadBalancerArn']},{load_balancer['Region']}\n")

    print(f'A list of load balancers without access logging has been saved to ./{csv_file_path}')
    print('All data has been saved to the current session.')

    print(f"{module_info['name']} completed.\n")
    return
