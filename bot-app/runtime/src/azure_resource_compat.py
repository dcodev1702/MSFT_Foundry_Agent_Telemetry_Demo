from __future__ import annotations


def get_resource_management_client_class():
    try:
        from azure.mgmt.resource import ResourceManagementClient
    except ImportError:
        from azure.mgmt.resource.resources import ResourceManagementClient

    return ResourceManagementClient
