import ipaddress
import json
import os
import subprocess
import time
import uuid

import waiting

from assisted_test_infra.test_infra.utils.waiting import wait_till_all_hosts_are_in_status
from deprecated_utils import wait_till_nodes_are_ready, are_libvirt_nodes_in_cluster_hosts, warn_deprecate
from assisted_test_infra.test_infra import utils
import consts
from assisted_test_infra.test_infra.tools import static_network, terraform_utils
from assisted_test_infra.test_infra.utils import TerraformControllerUtil

from service_client import log

warn_deprecate()


def set_cluster_pull_secret(client, cluster_id, pull_secret):
    client.set_pull_secret(cluster_id, pull_secret)


def execute_day2_cloud_flow(cluster_id, args, has_ipv6):
    execute_day2_flow(cluster_id, args, "cloud", has_ipv6)


def execute_day2_ocp_flow(cluster_id, args, has_ipv6):
    execute_day2_flow(cluster_id, args, "ocp", has_ipv6)


def execute_day2_flow(cluster_id, args, day2_type_flag, has_ipv6):
    utils.recreate_folder(consts.IMAGE_FOLDER, force_recreate=False)

    client = args.api_client
    cluster = client.cluster_get(cluster_id=cluster_id)
    cluster_name = cluster.name
    openshift_version = cluster.openshift_version
    api_vip_dnsname = "api." + cluster_name + "." + cluster.base_dns_domain
    api_vip_ip = cluster.api_vip
    terraform_cluster_dir_prefix = cluster_name
    if day2_type_flag == "ocp":
        terraform_cluster_dir_prefix = f"{consts.CLUSTER_PREFIX}-{consts.DEFAULT_NAMESPACE}"
    else:
        openshift_cluster_id = str(uuid.uuid4())
        cluster = client.create_day2_cluster(
            cluster_name + "-day2", openshift_cluster_id, **_day2_cluster_create_params(openshift_version, api_vip_dnsname)
        )
        set_cluster_pull_secret(client, cluster.id, args.pull_secret)
        set_cluster_proxy(client, cluster.id, args)

    config_etc_hosts(api_vip_ip, api_vip_dnsname)
    image_path = os.path.join(
        consts.IMAGE_FOLDER,
        f'{cluster_name}-installer-image.iso'
    )

    tf_folder = os.path.join(TerraformControllerUtil.get_folder(terraform_cluster_dir_prefix), consts.Platforms.BARE_METAL)
    set_day2_tf_configuration(tf_folder, args.num_day2_workers, api_vip_ip, api_vip_dnsname)

    static_network_config = None
    if args.with_static_network_config:
        static_network_config = static_network.generate_day2_static_network_data_from_tf(tf_folder, args.num_day2_workers)

    # Generate image
    infra_env = client.create_infra_env(
        cluster_id=cluster.id,
        name=cluster_name + "_infra-env",
        ssh_public_key=args.ssh_key,
        static_network_config=static_network_config,
        pull_secret=args.pull_secret,
        openshift_version=openshift_version
    )
    # Download image
    iso_download_url = infra_env.download_url
    log.info(f"Downloading image {iso_download_url} to {image_path}")
    utils.download_file(iso_download_url, image_path, False)

    day2_nodes_flow(
        client,
        terraform_cluster_dir_prefix,
        tf_folder,
        cluster,
        has_ipv6,
        args.num_day2_workers,
        args.install_cluster,
        day2_type_flag,
        args.with_static_network_config,
        cluster_name,
        infra_env.id
    )


def day2_nodes_flow(client,
                    terraform_cluster_dir_prefix,
                    tf_folder,
                    cluster,
                    has_ipv_6,
                    num_worker_nodes,
                    install_cluster_flag,
                    day2_type_flag,
                    with_static_network_config,
                    base_cluster_name,
                    infra_env_id):
    tf_network_name, total_num_nodes = get_network_num_nodes_from_tf(tf_folder)

    # Running twice as a workaround for an issue with terraform not spawning a new node on first apply.    
    for _ in range(2):
        with utils.file_lock_context():
            utils.run_command(
                f'make _apply_terraform CLUSTER_NAME={terraform_cluster_dir_prefix} PLATFORM={consts.Platforms.BARE_METAL}'
            )
    time.sleep(5)

    if day2_type_flag == "ocp":
        num_nodes_to_wait = total_num_nodes
        installed_status = consts.NodesStatus.INSTALLED
    else:
        num_nodes_to_wait = num_worker_nodes
        installed_status = consts.NodesStatus.DAY2_INSTALLED

    wait_till_nodes_are_ready(
        nodes_count=num_nodes_to_wait, network_name=tf_network_name
    )

    waiting.wait(
        lambda: are_libvirt_nodes_in_cluster_hosts(
            client, cluster.id, num_nodes_to_wait
        ),
        timeout_seconds=consts.NODES_REGISTERED_TIMEOUT,
        sleep_seconds=10,
        waiting_for="Nodes to be registered in inventory service",
    )

    set_nodes_hostnames_if_needed(client,
                                  tf_folder,
                                  with_static_network_config,
                                  has_ipv_6,
                                  tf_network_name,
                                  cluster.id)

    wait_till_all_hosts_are_in_status(
        client=client,
        cluster_id=cluster.id,
        nodes_count=num_worker_nodes,
        statuses=[
            consts.NodesStatus.KNOWN
        ],
        interval=30,
    )

    if install_cluster_flag:
        log.info("Start installing all known nodes in the cluster %s", cluster.id)
        kubeconfig = utils.get_kubeconfig_path(base_cluster_name)
        ocp_orig_ready_nodes = get_ocp_cluster_ready_nodes_num(kubeconfig)
        hosts = client.get_cluster_hosts(cluster.id)
        [client.install_day2_host(infra_env_id, host['id']) for host in hosts if host["status"] == 'known']

        log.info(
            "Start waiting until all nodes of cluster %s have been installed( reached added-to-existing-clustertate)",
            cluster.id)
        wait_till_all_hosts_are_in_status(
            client=client,
            cluster_id=cluster.id,
            nodes_count=num_nodes_to_wait,
            statuses=[
                installed_status
            ],
            interval=30,
        )

        log.info("Start waiting until installed nodes has actually been added to the OCP cluster")
        waiting.wait(
            lambda: wait_nodes_join_ocp_cluster(ocp_orig_ready_nodes, num_worker_nodes, day2_type_flag, kubeconfig),
            timeout_seconds=consts.NODES_REGISTERED_TIMEOUT,
            sleep_seconds=30,
            waiting_for="Day2 nodes to be added to OCP cluster",
            expected_exceptions=Exception
        )
        log.info("%d worker nodes were successfully added to OCP cluster", num_worker_nodes)


def set_hostnames_from_tf(client, cluster_id, tf_folder, network_name):
    tf = terraform_utils.TerraformUtils(working_dir=tf_folder)
    libvirt_nodes = utils.extract_nodes_from_tf_state(tf.get_state(), network_name, consts.NodeRoles.WORKER)
    utils.update_hosts(client, cluster_id, libvirt_nodes, update_roles=False, update_hostnames=True)


def set_day2_tf_configuration(tf_folder, num_worker_nodes, api_vip_ip, api_vip_dnsname):
    configure_terraform(tf_folder, num_worker_nodes, api_vip_ip, api_vip_dnsname)


def get_network_num_nodes_from_tf(tf_folder):
    tfvars = utils.get_tfvars(tf_folder)
    return tfvars['libvirt_network_name'], tfvars['master_count'] + tfvars['worker_count']


def configure_terraform(tf_folder, num_worker_nodes, api_vip_ip, api_vip_dnsname):
    tfvars = utils.get_tfvars(tf_folder)
    configure_terraform_workers_nodes(tfvars, num_worker_nodes)
    configure_terraform_api_dns(tfvars, api_vip_ip, api_vip_dnsname)
    utils.set_tfvars(tf_folder, tfvars)


def configure_terraform_workers_nodes(tfvars, num_worker_nodes):
    num_workers = tfvars['worker_count'] + num_worker_nodes
    tfvars['worker_count'] = num_workers
    set_workers_addresses_by_type(tfvars, num_worker_nodes, 'libvirt_master_ips', 'libvirt_worker_ips',
                                  'libvirt_worker_macs')
    set_workers_addresses_by_type(tfvars, num_worker_nodes, 'libvirt_secondary_master_ips',
                                  'libvirt_secondary_worker_ips', 'libvirt_secondary_worker_macs')


def configure_terraform_api_dns(tfvars, api_vip_ip, api_vip_dnsname):
    tfvars['api_vip'] = api_vip_ip


def set_workers_addresses_by_type(tfvars, num_worker_nodes, master_ip_type, worker_ip_type, worker_mac_type):
    old_worker_ips_list = tfvars[worker_ip_type]
    last_master_addresses = tfvars[master_ip_type][-1]

    if last_master_addresses:
        if old_worker_ips_list:
            worker_starting_ip = ipaddress.ip_address(old_worker_ips_list[-1][0])
        else:
            worker_starting_ip = ipaddress.ip_address(last_master_addresses[0])

        worker_ips_list = old_worker_ips_list + utils.create_ip_address_nested_list(num_worker_nodes,
                                                                                    worker_starting_ip + 1)
    else:
        log.info(
            "IPv6-only environment. IP addresses are left empty and will be allocated by libvirt "
            "DHCP because of a bug in Terraform plugin")
        worker_ips_list = old_worker_ips_list + utils.create_empty_nested_list(num_worker_nodes)

    tfvars[worker_ip_type] = worker_ips_list

    old_worker_mac_addresses = tfvars[worker_mac_type]
    tfvars[worker_mac_type] = old_worker_mac_addresses + static_network.generate_macs(num_worker_nodes)


def wait_nodes_join_ocp_cluster(num_orig_nodes, num_new_nodes, day2_type_flag, kubeconfig):
    if day2_type_flag == "cloud":
        approve_workers_on_ocp_cluster(kubeconfig)
    return get_ocp_cluster_ready_nodes_num(kubeconfig) == num_orig_nodes + num_new_nodes


def approve_workers_on_ocp_cluster(kubeconfig):
    csrs = get_ocp_cluster_csrs(kubeconfig)
    for csr in csrs:
        if not csr['status']:
            csr_name = csr['metadata']['name']
            ocp_cluster_csr_approve(csr_name, kubeconfig)
            log.info("CSR %s for node %s has been approved", csr_name, csr['spec']['username'])


def config_etc_hosts(api_vip_ip, api_vip_dnsname):
    with open("/etc/hosts", "r") as f:
        hosts_lines = f.readlines()
    for i, line in enumerate(hosts_lines):
        if api_vip_dnsname in line:
            hosts_lines[i] = api_vip_ip + " " + api_vip_dnsname + "\n"
            break
    else:
        hosts_lines.append(api_vip_ip + " " + api_vip_dnsname + "\n")
    with open("/etc/hosts", "w") as f:
        f.writelines(hosts_lines)


def get_ocp_cluster_nodes(kubeconfig):
    res = subprocess.check_output(f"oc --kubeconfig={kubeconfig} get nodes --output=json", shell=True)
    return json.loads(res)['items']


def is_ocp_node_ready(node_status):
    if not node_status:
        return False
    for condition in node_status['conditions']:
        if condition['status'] == 'True' and condition['type'] == 'Ready':
            return True
    return False


def get_ocp_cluster_ready_nodes_num(kubeconfig):
    nodes = get_ocp_cluster_nodes(kubeconfig)
    return len([node for node in nodes if is_ocp_node_ready(node['status'])])


def get_ocp_cluster_csrs(kubeconfig):
    res = subprocess.check_output(f'oc --kubeconfig={kubeconfig} get csr --output=json', shell=True)
    return json.loads(res)['items']


def ocp_cluster_csr_approve(csr_name, kubeconfig):
    subprocess.check_output(f'oc --kubeconfig={kubeconfig} adm certificate approve {csr_name}', shell=True)


def _day2_cluster_create_params(openshift_version, api_vip_dnsname):
    params = {
        "openshift_version": openshift_version,
        "api_vip_dnsname": api_vip_dnsname,
    }
    return params


def set_cluster_proxy(client, cluster_id, args):
    """
    Set cluster proxy - copy proxy configuration from another (e.g. day 1) cluster,
    or allow setting/overriding it via command arguments
    """
    http_proxy = getattr(args, "http_proxy", None)
    https_proxy = getattr(args, "https_proxy", None)
    no_proxy = getattr(args, "no_proxy", None)

    if http_proxy or https_proxy or no_proxy:
        client.set_cluster_proxy(cluster_id, http_proxy, https_proxy, no_proxy)


def set_nodes_hostnames_if_needed(client,
                                  tf_folder,
                                  with_static_network_config,
                                  has_ipv_6,
                                  network_name,
                                  cluster_id):
    if has_ipv_6 or with_static_network_config:
        tf = terraform_utils.TerraformUtils(working_dir=tf_folder)
        libvirt_nodes = utils.extract_nodes_from_tf_state(tf.get_state(), network_name, consts.NodeRoles.WORKER)
        log.info(
            "Set hostnames of day2 cluster %s in case of static network configuration or "
            "to work around libvirt for Terrafrom not setting hostnames of IPv6 hosts",
            cluster_id)
        utils.update_hosts(client, cluster_id, libvirt_nodes, update_roles=False, update_hostnames=True)