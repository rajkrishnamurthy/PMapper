"""Python code for gathering IAM-related information from an AWS account"""

#  Copyright (c) NCC Group and Erik Steringer 2019. This file is part of Principal Mapper.
#
#      Principal Mapper is free software: you can redistribute it and/or modify
#      it under the terms of the GNU Affero General Public License as published by
#      the Free Software Foundation, either version 3 of the License, or
#      (at your option) any later version.
#
#      Principal Mapper is distributed in the hope that it will be useful,
#      but WITHOUT ANY WARRANTY; without even the implied warranty of
#      MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#      GNU Affero General Public License for more details.
#
#      You should have received a copy of the GNU Affero General Public License
#      along with Principal Mapper.  If not, see <https://www.gnu.org/licenses/>.

import io
import os

import botocore.session
import principalmapper
from principalmapper.common import Node, Group, Policy, Graph
from principalmapper.graphing import edge_identification
from principalmapper.querying import query_interface
from principalmapper.util import arns
from principalmapper.util.debug_print import dprint
from typing import List, Optional


def create_graph(session: botocore.session.Session, service_list: list, output: io.StringIO = os.devnull,
                 debug=False) -> Graph:
    """Constructs a Graph object.

    Information about the graph as it's built will be written to the IO parameter `output`.
    """
    stsclient = session.create_client('sts')
    caller_identity = stsclient.get_caller_identity()
    dprint(debug, "Caller Identity: {}".format(caller_identity['Arn']))
    metadata = {
        'account_id': caller_identity['Account'],
        'pmapper_version': principalmapper.__version__
    }

    iamclient = session.create_client('iam')

    results = get_nodes_groups_and_policies(iamclient, output, debug)
    nodes_result = results['nodes']
    groups_result = results['groups']
    policies_result = results['policies']

    # Gather users and roles, generating a Node per user and per role
    # nodes_result = get_unfilled_nodes(iamclient, output, debug)

    # Gather groups from current list of nodes (users), generate Group objects, attach to nodes in-flight
    # groups_result = get_unfilled_groups(iamclient, nodes_result, output, debug)

    # Resolve all policies, generate Policy objects, attach to all groups and nodes
    # policies_result = get_policies_and_fill_out(iamclient, nodes_result, groups_result, output, debug)

    # Determine which nodes are admins and update node objects
    update_admin_status(nodes_result, output, debug)

    # Generate edges, generate Edge objects
    edges_result = edge_identification.obtain_edges(session, service_list, nodes_result, output, debug)

    return Graph(nodes_result, edges_result, policies_result, groups_result, metadata)


def get_nodes_groups_and_policies(iamclient, output: io.StringIO = os.devnull, debug=False) -> dict:
    """Using an IAM.Client object, return a dictionary containing nodes, groups, and policies to be
    added to a Graph object. Admin status for the nodes are not updated.

    Writes high-level information on progress to the output stream.
    """
    output.write('Obtaining IAM Users/Roles/Groups/Policies in the account.\n')
    result_paginator = iamclient.get_paginator('get_account_authorization_details')
    user_results = []
    group_results = []
    role_results = []
    policy_results = []
    for page in result_paginator.paginate():
        if debug:
            output.write('{}\n'.format(page))
        user_results += page['UserDetailList']
        group_results += page['GroupDetailList']
        role_results += page['RoleDetailList']
        policy_results += page['Policies']

    output.write('Sorting users, roles, groups, policies, and their relationships.\n')

    # this is the result we return: dictionary with nodes/groups/users all filled out
    result = {
        'nodes': [],
        'groups': [],
        'policies': []
    }

    for p in policy_results:
        # go through each policy and update policy_results
        doc = [x['Document'] for x in p['PolicyVersionList'] if x['IsDefaultVersion']][0]
        result['policies'].append(
            Policy(
                p['Arn'],
                p['PolicyName'],
                doc
            )
        )

    for g in group_results:
        # go through all inline policies and update policy_results
        group_policies = []
        if 'GroupPolicyList' in g:  # have to key-check these
            for p in g['GroupPolicyList']:
                group_policies.append(
                    Policy(
                        g['Arn'],  # inline policies get the same Arn as their principal
                        p['PolicyName'],
                        p['PolicyDocument']
                    )
                )
            result['policies'] += group_policies  # this is just adding the inline policies for the group

        for p in g['AttachedManagedPolicies']:
            group_policies.append(_get_policy_by_arn_or_raise(p['PolicyArn'], result['policies']))

        result['groups'].append(
            Group(
                g['Arn'],
                group_policies
            )
        )

    for u in user_results:
        # go through all inline policies and update policy_results
        user_policies = []
        if 'UserPolicyList' in u:  # have to key-check these
            for p in u['UserPolicyList']:
                user_policies.append(
                    Policy(
                        u['Arn'],  # inline policies inherit the Arn of their principal for the purposes of tracking
                        p['PolicyName'],
                        p['PolicyDocument']
                    )
                )
            result['policies'] += user_policies

        for p in u['AttachedManagedPolicies']:
            user_policies.append(_get_policy_by_arn_or_raise(p['PolicyArn'], result['policies']))

        if 'PermissionsBoundary' in u:
            boundary_policy = _get_policy_by_arn_or_raise(u['PermissionsBoundary']['PermissionsBoundaryArn'],
                                                          result['policies'])
        else:
            boundary_policy = None

        group_list = []
        for group_name in u['GroupList']:
            for group in result['groups']:
                if arns.get_resource(group.arn).split('/')[-1] == group_name:
                    group_list.append(group)
                    break

        # still need to figure out access keys
        result['nodes'].append(
            Node(
                u['Arn'], u['UserId'], user_policies, group_list, None, None, 0, 'PasswordLastUsed' in u, False,
                boundary_policy, False
            )
        )

    for r in role_results:
        # go through all inline policies and update policy_results
        role_policies = []
        for p in r['RolePolicyList']:
            role_policies.append(
                Policy(
                    r['Arn'],  # inline policies inherit the Arn of their principal for the purposes of tracking
                    p['PolicyName'],
                    p['PolicyDocument']
                )
            )
        result['policies'] += role_policies

        for p in r['AttachedManagedPolicies']:
            role_policies.append(_get_policy_by_arn_or_raise(p['PolicyArn'], result['policies']))

        result['nodes'].append(
            Node(
                r['Arn'], r['RoleId'], role_policies, None, r['AssumeRolePolicyDocument'],
                [x['Arn'] for x in r['InstanceProfileList']], 0, False, False,
                None, False
            )
        )

    output.write("Obtaining Access Keys data for IAM users\n")
    for node in result['nodes']:
        if arns.get_resource(node.arn).startswith('user/'):
            # Grab access-key count and update node
            user_name = arns.get_resource(node.arn)[5:]
            if '/' in user_name:
                user_name = user_name.split('/')[-1]
                dprint(debug, 'removed path from username {}'.format(user_name))
            access_keys_data = iamclient.list_access_keys(UserName=user_name)
            node.access_keys = len(access_keys_data['AccessKeyMetadata'])
            dprint(debug, 'Access Key Count for {}: {}'.format(user_name, len(access_keys_data['AccessKeyMetadata'])))

    output.write('Gathering MFA virtual device information\n')
    mfa_paginator = iamclient.get_paginator('list_virtual_mfa_devices')
    for page in mfa_paginator.paginate(AssignmentStatus='Assigned'):
        for device in page['VirtualMFADevices']:
            user_arn = device['User']['Arn']
            for node in result['nodes']:
                if node.arn == user_arn:
                    node.has_mfa = True
                    break

    return result


def get_unfilled_nodes(iamclient, output: io.StringIO = os.devnull, debug=False) -> List[Node]:
    """Using an IAM.Client object, return a list of Node object for each IAM user and role in an account.

    Does not set Group or Policy objects, does not set permissions boundary attr. Those have to be filled in later.

    Writes high-level information on progress to the output file
    """
    result = []
    # Get users, paginating results, still need to handle policies + group memberships + is_admin
    output.write("Obtaining IAM users in account\n")
    user_paginator = iamclient.get_paginator('list_users')
    for page in user_paginator.paginate(PaginationConfig={'PageSize': 25}):
        dprint(debug, 'list_users page: {}'.format(page))
        for user in page['Users']:
            # grab permission boundary ARN if applicable
            # TODO: iam:ListUsers does not return boundary information. may need to wait for a fix.
            if 'PermissionsBoundary' in user:
                _pb = user['PermissionsBoundary']['PermissionsBoundaryArn']
            else:
                _pb = None
            result.append(Node(
                arn=user['Arn'],
                id_value=user['UserId'],
                attached_policies=[],
                group_memberships=[],
                trust_policy=None,
                instance_profile=None,
                num_access_keys=0,
                active_password='PasswordLastUsed' in user,
                is_admin=False,
                permissions_boundary=_pb,
                has_mfa=False
            ))
            dprint(debug, 'Adding Node for user ' + user['Arn'])

    # Get roles, paginating results, still need to handle policies + is_admin
    output.write("Obtaining IAM roles in account\n")
    role_paginator = iamclient.get_paginator('list_roles')
    for page in role_paginator.paginate(PaginationConfig={'PageSize': 25}):
        dprint(debug, 'list_roles page: {}'.format(page))
        for role in page['Roles']:
            # grab permission boundary ARN if applicable
            if 'PermissionsBoundary' in role:
                _pb = role['PermissionsBoundary']['PermissionsBoundaryArn']
            else:
                _pb = None
            result.append(Node(
                arn=role['Arn'],
                id_value=role['RoleId'],
                attached_policies=[],
                group_memberships=[],
                trust_policy=role['AssumeRolePolicyDocument'],
                instance_profile=None,
                num_access_keys=0,
                active_password=False,
                is_admin=False,
                permissions_boundary=_pb,
                has_mfa=False
            ))

    # Get instance profiles, paginating results, and attach to roles as appropriate
    output.write("Obtaining EC2 instance profiles in account\n")
    ip_paginator = iamclient.get_paginator('list_instance_profiles')
    for page in ip_paginator.paginate(PaginationConfig={'PageSize': 25}):
        dprint(debug, 'list_instance_profiles page: {}'.format(page))
        for iprofile in page['InstanceProfiles']:
            iprofile_arn = iprofile['Arn']
            role_arns = []
            for role in iprofile['Roles']:
                role_arns.append(role['Arn'])
            for node in result:
                if ':role/' in node.arn and node.arn in role_arns:
                    node.instance_profile = iprofile_arn

    # Handle access keys
    output.write("Obtaining Access Keys data for IAM users\n")
    for node in result:
        if arns.get_resource(node.arn).startswith('user/'):
            # Grab access-key count and update node
            user_name = arns.get_resource(node.arn)[5:]
            if '/' in user_name:
                user_name = user_name.split('/')[-1]
                dprint(debug, 'removed path from username {}'.format(user_name))
            access_keys_data = iamclient.list_access_keys(UserName=user_name)
            node.access_keys = len(access_keys_data['AccessKeyMetadata'])
            dprint(debug, 'Access Key Count for {}: {}'.format(user_name, len(access_keys_data['AccessKeyMetadata'])))

    return result


def get_unfilled_groups(iamclient, nodes: List[Node], output: io.StringIO = os.devnull, debug=False) -> List[Group]:
    """Using an IAM.Client object, returns a list of Group objects. Adds to each passed Node's group_memberships
    property.

    Does not set Policy objects. Those have to be filled in later.

    Writes high-level progress information to parameter output
    """
    result = []

    # paginate through groups and build result
    output.write("Obtaining IAM groups in the account.\n")
    group_paginator = iamclient.get_paginator('list_groups')
    for page in group_paginator.paginate(PaginationConfig={'PageSize': 25}):
        dprint(debug, 'list_groups page: {}'.format(page))
        for group in page['Groups']:
            result.append(Group(
                arn=group['Arn'],
                attached_policies=[]
            ))

    # loop through group memberships
    output.write("Connecting IAM users to their groups.\n")
    for node in nodes:
        if not arns.get_resource(node.arn).startswith('user/'):
            continue  # skip when not an IAM user
        dprint(debug, 'finding groups for user {}'.format(node.arn))
        user_name = arns.get_resource(node.arn)[5:]
        if '/' in user_name:
            user_name = user_name.split('/')[-1]
            dprint(debug, 'removed path from username {}'.format(user_name))
        group_list = iamclient.list_groups_for_user(UserName=user_name)
        for group in group_list['Groups']:
            for group_obj in result:
                if group['Arn'] == group_obj.arn:
                    node.group_memberships.append(group_obj)

    return result


def get_policies_and_fill_out(iamclient, nodes: List[Node], groups: List[Group],
                              output: io.StringIO = os.devnull, debug=False) -> List[Policy]:
    """Using an IAM.Client object, return a list of Policy objects. Adds references to each passed Node and
    Group object where applicable. Updates boundary policies.

    Writes high-level progress information to parameter output.
    """
    result = []

    # navigate through nodes and add policy objects if they do not already exist in result
    output.write("Obtaining policies used by all IAM users and roles\n")
    for node in nodes:
        node_name_components = arns.get_resource(node.arn).split('/')
        node_type, node_name = node_name_components[0], node_name_components[-1]
        dprint(debug, 'Grabbing inline policies for {}'.format(node.arn))
        # get inline policies
        if node_type == 'user':
            inline_policy_arns = iamclient.list_user_policies(UserName=node_name)
            # get each inline policy, append it to node's policies and result list
            for policy_name in inline_policy_arns['PolicyNames']:
                dprint(debug, '   Grabbing inline policy: {}'.format(policy_name))
                inline_policy = iamclient.get_user_policy(UserName=node_name, PolicyName=policy_name)
                policy_object = Policy(arn=node.arn, name=policy_name, policy_doc=inline_policy['PolicyDocument'])
                node.attached_policies.append(policy_object)
                result.append(policy_object)
        elif node_type == 'role':
            inline_policy_arns = iamclient.list_role_policies(RoleName=node_name)
            # get each inline policy, append it to the node's policies and result list
            # in hindsight, it's possible this could be folded with the above code, assuming the API doesn't change
            for policy_name in inline_policy_arns['PolicyNames']:
                dprint(debug, '   Grabbing inline policy: {}'.format(policy_name))
                inline_policy = iamclient.get_role_policy(RoleName=node_name, PolicyName=policy_name)
                policy_object = Policy(arn=node.arn, name=policy_name, policy_doc=inline_policy['PolicyDocument'])
                node.attached_policies.append(policy_object)
                result.append(policy_object)

        # get attached policies for users and roles
        if node_type == 'user':
            attached_policies = iamclient.list_attached_user_policies(UserName=node_name)
        else:  # node_type == 'role':
            attached_policies = iamclient.list_attached_role_policies(RoleName=node_name)
        for attached_policy in attached_policies['AttachedPolicies']:
            policy_arn = attached_policy['PolicyArn']
            dprint(debug, '   Grabbing managed policy: {}'.format(policy_arn))
            # reduce API calls, search existing policies for matching arns
            policy_object = _get_policy_by_arn(policy_arn, result)
            if policy_object is None:
                # Gotta retrieve the policy's current default version
                dprint(debug, '      Policy cache miss, calling API')
                policy_response = iamclient.get_policy(PolicyArn=policy_arn)
                dprint(debug, '      Policy version: {}'.format(policy_response['Policy']['DefaultVersionId']))
                policy_version_response = iamclient.get_policy_version(
                    PolicyArn=policy_arn,
                    VersionId=policy_response['Policy']['DefaultVersionId']
                )
                policy_object = Policy(
                    arn=policy_arn,
                    name=policy_response['Policy']['PolicyName'],
                    policy_doc=policy_version_response['PolicyVersion']['Document']
                )
                result.append(policy_object)
            node.attached_policies.append(policy_object)

        # get permission boundaries for users/roles
        dprint(debug,   "perm boundary of {}: {}".format(node.searchable_name(), node.permissions_boundary))
        if node.permissions_boundary is not None and isinstance(node.permissions_boundary, str):
            dprint(debug, '      Getting boundary policy: {}'.format(node.permissions_boundary))
            # reduce API calls, search existing policies for matching ARNs
            policy_object = _get_policy_by_arn(node.permissions_boundary, result)
            if policy_object is None:
                # Retrieve the policy's current default version
                dprint(debug, '      Policy cache miss, calling API')
                policy_response = iamclient.get_policy(PolicyArn=node.permissions_boundary)
                dprint(debug, '      Policy version: {}'.format(policy_response['Policy']['DefaultVersionId']))
                policy_version_response = iamclient.get_policy_version(
                    PolicyArn=node.permissions_boundary,
                    VersionId=policy_response['Policy']['DefaultVersionId']
                )
                policy_object = Policy(
                    arn=node.permissions_boundary,
                    name=policy_response['Policy']['PolicyName'],
                    policy_doc=policy_version_response['PolicyVersion']['Document']
                )
                result.append(policy_object)
                node.permissions_boundary = policy_object

    output.write("Obtaining policies used by IAM groups\n")
    for group in groups:
        group_name = arns.get_resource(group.arn).split('/')[-1]  # split by slashes and take the final item
        dprint(debug, 'Getting policies for: {}'.format(group.arn))
        # get inline policies
        inline_policies = iamclient.list_group_policies(GroupName=group_name)
        for policy_name in inline_policies['PolicyNames']:
            dprint(debug, '   Grabbing inline policy: {}'.format(policy_name))
            inline_policy = iamclient.get_group_policy(GroupName=group_name, PolicyName=policy_name)
            policy_object = Policy(arn=group.arn, name=policy_name, policy_doc=inline_policy['PolicyDocument'])
            group.attached_policies.append(policy_object)
            result.append(policy_object)

        # get attached policies
        attached_policies = iamclient.list_attached_group_policies(GroupName=group_name)
        for attached_policy in attached_policies['AttachedPolicies']:
            policy_arn = attached_policy['PolicyArn']
            dprint(debug, '   Grabbing managed policy: {}'.format(policy_arn))
            # check cached policies first
            policy_object = _get_policy_by_arn(policy_arn, result)
            if policy_object is None:
                dprint(debug, '      Policy cache miss, calling API')
                policy_response = iamclient.get_policy(PolicyArn=policy_arn)
                dprint(debug, '      Policy version: {}'.format(policy_response['Policy']['DefaultVersionId']))
                policy_version_response = iamclient.get_policy_version(
                    PolicyArn=policy_arn,
                    VersionId=policy_response['Policy']['DefaultVersionId']
                )
                policy_object = Policy(
                    arn=policy_arn,
                    name=policy_response['Policy']['PolicyName'],
                    policy_doc=policy_version_response['PolicyVersion']['Document']
                )
                result.append(policy_object)
            group.attached_policies.append(policy_object)

    return result


def update_admin_status(nodes: List[Node], output: io.StringIO = os.devnull, debug: bool = False) -> None:
    """Given a list of nodes, goes through and updates each node's is_admin data."""
    for node in nodes:
        output.write("checking if {} is an admin\n".format(node.searchable_name()))
        node_type = arns.get_resource(node.arn).split('/')[0]

        # check if node can modify its own inline policies
        if node_type == 'user':
            action = 'iam:PutUserPolicy'
        else:  # node_type == 'role'
            action = 'iam:PutRolePolicy'
        if query_interface.local_check_authorization_handling_mfa(node, action, node.arn, {}, debug)[0]:
            node.is_admin = True
            continue

        # check if node can attach the AdministratorAccess policy to itself
        if node_type == 'user':
            action = 'iam:AttachUserPolicy'
        else:
            action = 'iam:AttachRolePolicy'
        condition_keys = {'iam:PolicyARN': 'arn:aws:iam::aws:policy/AdministratorAccess'}
        if query_interface.local_check_authorization_handling_mfa(node, action, node.arn, condition_keys, debug)[0]:
            node.is_admin = True
            continue

        # check if node can create a role and attach the AdministratorAccess policy or an inline policy
        if query_interface.local_check_authorization_handling_mfa(node, 'iam:CreateRole', '*', {}, debug)[0]:
            if query_interface.local_check_authorization_handling_mfa(node, 'iam:AttachRolePolicy', '*',
                                                                      condition_keys, debug)[0]:
                node.is_admin = True
                continue
            if query_interface.local_check_authorization_handling_mfa(node, 'iam:PutRolePolicy', '*', condition_keys,
                                                                      debug)[0]:
                node.is_admin = True
                continue

        # check if node can update an attached customer-managed policy (assumes SetAsDefault is set to True)
        for attached_policy in node.attached_policies:
            if attached_policy.arn != node.arn:
                if query_interface.local_check_authorization_handling_mfa(node, 'iam:CreatePolicyVersion',
                                                                          attached_policy.arn, {}, debug)[0]:
                    node.is_admin = True
                    continue

        # check if node is a user, and if it can attach or modify any of its groups's policies
        if node_type == 'user':
            for group in node.group_memberships:
                if query_interface.local_check_authorization_handling_mfa(node, 'iam:PutGroupPolicy', group.arn, {},
                                                                          debug)[0]:
                    node.is_admin = True
                    break  # break the loop through groups
                if query_interface.local_check_authorization_handling_mfa(node, 'iam:AttachGroupPolicy', group.arn,
                                                                          condition_keys, debug)[0]:
                    node.is_admin = True
                    break  # as above
                for attached_policy in group.attached_policies:
                    if attached_policy.arn != group.arn:
                        if query_interface.local_check_authorization_handling_mfa(node, 'iam:CreatePolicyVersion',
                                                                                  attached_policy.arn, {}, debug)[0]:
                            node.is_admin = True
                            break  # break the loop through policies
                if node.is_admin:
                    break  # break the loop through groups
            if node.is_admin:
                continue  # if we add more checks later, this optimizes them out when appropriate


def _get_policy_by_arn(arn: str, policies: List[Policy]) -> Optional[Policy]:
    """Helper function: pull a Policy object with the same ARN from a list or return None"""
    for policy in policies:
        if arn == policy.arn:
            return policy
    return None


def _get_policy_by_arn_or_raise(arn: str, policies: List[Policy]) -> Policy:
    """Helper function: pull a Policy object with the same ARN from a List, or raise a ValueError"""
    for policy in policies:
        if arn == policy.arn:
            return policy
    raise ValueError('Could not locate policy {}.'.format(arn))
