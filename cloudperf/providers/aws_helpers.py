from __future__ import absolute_import
import base64
import re
import json
import time
import threading
import logging
from logging import NullHandler
import copy
from datetime import datetime
from io import StringIO
from multiprocessing.pool import ThreadPool
import boto3
import cachetools
import requests
import paramiko
import pandas as pd
from dateutil import parser
from botocore.exceptions import ClientError
from cloudperf.benchmarks import benchmarks

session = boto3.session.Session()
logger = logging.getLogger(__name__)
logger.addHandler(NullHandler())

# static map until Amazon can provide the name in boto3 along with the region
# code...
region_map = {
    'Asia Pacific (Mumbai)': 'ap-south-1',
    'Asia Pacific (Seoul)': 'ap-northeast-2',
    'Asia Pacific (Singapore)': 'ap-southeast-1',
    'Asia Pacific (Sydney)': 'ap-southeast-2',
    'Asia Pacific (Tokyo)': 'ap-northeast-1',
    'Canada (Central)': 'ca-central-1',
    'EU (Frankfurt)': 'eu-central-1',
    'EU (Ireland)': 'eu-west-1',
    'EU (London)': 'eu-west-2',
    'EU (Paris)': 'eu-west-3',
    'South America (Sao Paulo)': 'sa-east-1',
    'US East (N. Virginia)': 'us-east-1',
    'US East (Ohio)': 'us-east-2',
    'AWS GovCloud (US)': 'us-gov-west-1',
    'AWS GovCloud (US-West)': 'us-gov-west-1',
    'AWS GovCloud (US-East)': 'us-gov-east-1',
    'US West (N. California)': 'us-west-1',
    'US West (Oregon)': 'us-west-2'}
# Self-destruct the machine after 2 hours
userdata_script="""#!/bin/sh
shutdown +120"""
ssh_keyname = 'batch'
ssh_user = 'ec2-user'
ssh_get_conn_timeout = 600
ec2_specs = {'KeyName': ssh_keyname, 'SecurityGroups': ['tech-ssh'],
             'MaxCount': 1, 'MinCount': 1, 'Monitoring': {'Enabled': False},
             'InstanceInitiatedShutdownBehavior': 'terminate',
             'UserData': userdata_script,
             'TagSpecifications': [{'ResourceType': 'instance',
                                    'Tags': [{'Value': 'cloudperf', 'Key': 'Application'}]},
                                   {'ResourceType': 'volume',
                                    'Tags': [{'Value': 'cloudperf', 'Key': 'Application'}]}]}

stop_services_cmd = "sudo systemctl | grep running | awk '{print $1}' | egrep -v '(auditd|dbus|docker|syslog|sshd|systemd|\.scope|network\.service)' | xargs sudo systemctl stop"


class DictQuery(dict):
    def get(self, keys, default=None):
        val = None

        for key in keys:
            if val:
                if isinstance(val, list):
                    val = [v.get(key, default) if v else None for v in val]
                else:
                    try:
                        val = val.get(key, default)
                    except AttributeError:
                        return default
            else:
                val = dict.get(self, key, default)

            if val == default:
                break

        return val


def boto3_paginate(method, **kwargs):
    client = method.__self__
    paginator = client.get_paginator(method.__name__)
    for page in paginator.paginate(**kwargs).result_key_iters():
        for result in page:
            yield result


def ping_region(region, latencies, lock):
    st = time.time()
    try:
        requests.get('http://ec2.{}.amazonaws.com/ping'.format(region), timeout=1)
    except Exception:
        return
    with lock:
        latencies[region] = time.time()-st


def aws_ping(regions):
    latencies = {}
    lock = threading.Lock()
    threads = []
    for region in regions:
        t = threading.Thread(target=ping_region, args=(region, latencies, lock))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return latencies


@cachetools.cached(cache={})
def aws_get_parameter(name):
    ssm = session.client('ssm', region_name=aws_get_region())
    res = ssm.get_parameter(Name=name, WithDecryption=True)
    try:
        return json.loads(res['Parameter']['Value'])
    except Exception:
        return res['Parameter']['Value']


def aws_get_cpu_arch(instance):
    # XXX: maybe in the future Amazon will indicate the exact CPU architecture,
    # but until that...
    physproc = DictQuery(instance).get(['product', 'attributes', 'physicalProcessor'], '').lower()
    procarch = DictQuery(instance).get(['product', 'attributes', 'processorArchitecture'], '').lower()
    instance_type = DictQuery(instance).get(['product', 'attributes', 'instanceType'], '').lower()

    if re.match('^a[0-9]+\.', instance_type) or re.search('aws\s+(graviton|)\s*processor', physproc):
        # try to find arm instances
        return 'arm64'

    return 'x86_64'


def aws_get_region():
    region = boto3.session.Session().region_name
    if region:
        return region
    try:
        r = requests.get(
            'http://169.254.169.254/latest/dynamic/instance-identity/document')
        return r.json().get('region')
    except Exception:
        return None


def aws_newest_image(imgs):
    latest = None

    for image in imgs:
        if not latest:
            latest = image
            continue

        if parser.parse(image['CreationDate']) > parser.parse(latest['CreationDate']):
            latest = image

    return latest


@cachetools.cached(cache={})
def aws_get_latest_ami(name='amzn2-ami-ecs-hvm*ebs', arch='x86_64'):
    ec2 = session.client('ec2', region_name=aws_get_region())

    filters = [
        {'Name': 'name', 'Values': [name]},
        {'Name': 'description', 'Values': ['Amazon Linux AMI*']},
        {'Name': 'architecture', 'Values': [arch]},
        {'Name': 'owner-alias', 'Values': ['amazon']},
        {'Name': 'state', 'Values': ['available']},
        {'Name': 'root-device-type', 'Values': ['ebs']},
        {'Name': 'virtualization-type', 'Values': ['hvm']},
        {'Name': 'image-type', 'Values': ['machine']}
    ]

    response = ec2.describe_images(Owners=['amazon'], Filters=filters)
    return aws_newest_image(response['Images'])


@cachetools.cached(cache={}, key=tuple)
def closest_regions(regions):
    latencies = aws_ping(regions)
    regions.sort(key=lambda k: latencies.get(k, 9999))
    return regions


def aws_format_memory(memory):
    return "{:,g} GiB".format(float(memory))


def aws_parse_memory(memory):
    # currently only GiBs are returned, so we don't need to take unit into account
    number, unit = memory.split()
    return float(number.replace(',', ''))


@cachetools.cached(cache={})
def get_region():
    region = boto3.session.Session().region_name
    if region:
        return region
    try:
        r = requests.get(
            'http://169.254.169.254/latest/dynamic/instance-identity/document')
        return r.json().get('region')
    except Exception:
        return None


@cachetools.cached(cache={})
def get_regions():
    client = session.client('ec2')
    return [region['RegionName'] for region in client.describe_regions()['Regions']]


@cachetools.cached(cache={})
def get_ec2_instances(**filter_opts):
    """Get AWS instances according to the given filter criteria

    Args:
        any Field:Value pair which the AWS API accepts.
        Example from a c5.4xlarge instance:
        {'capacitystatus': 'Used',
         'clockSpeed': '3.0 Ghz',
         'currentGeneration': 'Yes',
         'dedicatedEbsThroughput': 'Upto 2250 Mbps',
         'ecu': '68',
         'enhancedNetworkingSupported': 'Yes',
         'instanceFamily': 'Compute optimized',
         'instanceType': 'c5.4xlarge',
         'licenseModel': 'No License required',
         'location': 'US West (Oregon)',
         'locationType': 'AWS Region',
         'memory': '32 GiB',
         'networkPerformance': 'Up to 10 Gigabit',
         'normalizationSizeFactor': '32',
         'operatingSystem': 'Linux',
         'operation': 'RunInstances:0004',
         'physicalProcessor': 'Intel Xeon Platinum 8124M',
         'preInstalledSw': 'SQL Std',
         'processorArchitecture': '64-bit',
         'processorFeatures': 'Intel AVX, Intel AVX2, Intel AVX512, Intel Turbo',
         'servicecode': 'AmazonEC2',
         'servicename': 'Amazon Elastic Compute Cloud',
         'storage': 'EBS only',
         'tenancy': 'Host',
         'usagetype': 'USW2-HostBoxUsage:c5.4xlarge',
         'vcpu': '16'}

    Returns:
        type: dict of AWS product descriptions

    """
    filters = [{'Type': 'TERM_MATCH', 'Field': k, 'Value': v}
               for k, v in filter_opts.items()]

    # currently the pricing API is limited to some regions, so don't waste time
    # on trying to access it on others one by one
    # regions = get_regions()
    regions = ['us-east-1', 'ap-south-1']
    for region in closest_regions(regions):
        pricing = session.client('pricing', region_name=region)
        instances = []
        for data in boto3_paginate(pricing.get_products, ServiceCode='AmazonEC2', Filters=filters, MaxResults=100):
            pd = json.loads(data)
            instances.append(pd)
        break
    return instances


def get_ec2_prices(**filter_opts):
    """Get AWS instance prices according to the given filter criteria

    Args:
        get_instance_types arguments

    Returns:
        DataFrame with instance attributes and pricing

    """
    prices = []
    params = {}

    for data in get_ec2_instances(**filter_opts):
        try:
            instance_type = data['product']['attributes']['instanceType']
            price = float(list(list(data['terms']['OnDemand'].values())[
                          0]['priceDimensions'].values())[0]['pricePerUnit']['USD'])
        except Exception:
            continue
        if price == 0:
            continue
        if data['product']['attributes']['memory'] == 'NA' or \
                data['product']['attributes']['vcpu'] == 'NA':
            # skip these
            continue
        vcpu = int(data['product']['attributes']['vcpu'])
        memory = aws_parse_memory(data['product']['attributes']['memory'])
        region = region_map.get(data['product']['attributes']['location'])
        params[instance_type] = data['product']['attributes']
        params[instance_type].update({'vcpu': vcpu, 'memory': memory, 'region': region,
                                      'cpu_arch': aws_get_cpu_arch(data),
                                      'date': datetime.now()})
        d = {'price': price, 'spot': False, 'spot-az': None}
        d.update(params[instance_type])
        prices.append(d)

    if not prices:
        # we couldn't find any matching instances
        return prices

    for region in get_regions():
        ec2 = session.client('ec2', region_name=region)
        for data in boto3_paginate(ec2.describe_spot_price_history, InstanceTypes=list(params.keys()),
                                   MaxResults=100, ProductDescriptions=['Linux/UNIX (Amazon VPC)'], StartTime=datetime.now()):
            instance_type = data['InstanceType']
            d = copy.deepcopy(params[instance_type])
            d.update({'price': float(data['SpotPrice']), 'spot': True, 'spot-az': data['AvailabilityZone'], 'region': region})
            prices.append(d)

    return pd.DataFrame.from_dict(prices)


def get_ssh_connection(instance, user, pkey, timeout):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    start = time.time()
    while start+timeout > time.time():
        try:
            ssh.connect(instance['PrivateIpAddress'], username=user, pkey=pkey, timeout=10, auth_timeout=10)
            break
        except Exception as e:
            logger.info("Couldn't connect to {}, {}, retrying for {:.0f}s".format(
                instance['InstanceId'], e, start+timeout-time.time()))
            time.sleep(5)
    else:
        return None
    return ssh


def run_benchmarks(args):
    ami, instance, benchmarks_to_run = args
    specs = copy.deepcopy(ec2_specs)
    bdmap = ami['BlockDeviceMappings']
    try:
        # You cannot specify the encrypted flag if specifying a snapshot id in a block device mapping.
        del bdmap[0]['Ebs']['Encrypted']
    except Exception:
        pass
    specs.update({'BlockDeviceMappings': bdmap,
                  'ImageId': ami['ImageId'], 'InstanceType': instance.instanceType})
    # add unlimited cpu credits on burstable type instances, so these won't affect
    # benchmark results
    if re.match('^t[0-9]+\.', instance.instanceType):
        specs.update({'CreditSpecification': {'CpuCredits': 'unlimited'}})
    spotspecs = copy.deepcopy(specs)
    spotspecs.update({'InstanceMarketOptions': {'MarketType': 'spot',
                                                'SpotOptions': {
                                                    'MaxPrice': str(instance.price),
                                                    'SpotInstanceType': 'one-time',
                                                    'InstanceInterruptionBehavior': 'terminate'
                                                    }
                                                }})
    # start with a spot instance
    create_specs = spotspecs
    retcount = 0
    ec2_inst = None
    ec2 = session.client('ec2', region_name=aws_get_region())
    while retcount < 16:
        try:
            ec2_inst = ec2.run_instances(**create_specs)['Instances'][0]
            break
        except ClientError as e:
            # retry on request limit exceeded
            if e.response['Error']['Code'] == 'RequestLimitExceeded':
                logger.info("Request limit for {}: {}, retry #{}".format(instance.instanceType,
                                                                           e.response['Error']['Message'], retcount))
                time.sleep(1.2**retcount)
                retcount += 1
                continue

            if e.response['Error']['Code'] == 'InsufficientInstanceCapacity':
                logger.info('Insufficient spot capacity for {}: {}'.format(
                    instance.instanceType, e))
                # retry with on demand
                create_specs = specs
                retcount = 0
                continue

            if e.response['Error']['Code'] == 'SpotMaxPriceTooLow':
                try:
                    # the actual spot price is the second, extract it
                    sp = re.findall('[0-9]+\.[0-9]+',
                                    e.response['Error']['Message'])[1]
                    logger.info(
                        "Spot price too low spotmax:{}, current price:{}".format(instance.price, sp))
                except Exception:
                    logger.info("Spot price too low for {}, {}".format(
                        instance.instanceType, e.response['Error']['Message']))
                # retry with on demand
                create_specs = specs
                retcount = 0
                continue

            if e.response['Error']['Code'] == 'MissingParameter':
                # this can't be fixed, exit
                logger.error("Missing parameter while creating {}: {}".format(
                    instance.instanceType, e))
                break

            if e.response['Error']['Code'] == 'InvalidParameterValue':
                # certain instances are not allowed to be created
                logger.error("Error starting instance {}: {}".format(
                    instance.instanceType, e.response['Error']['Message']))
                break

            if e.response['Error']['Code'] == 'Unsupported':
                # certain instances are not allowed to be created
                logger.error("Unsupported instance {}: {}, specs: {}".format(
                    instance.instanceType, e.response['Error']['Message'],
                    base64.b64encode(str(create_specs))))
                break

            logger.error("Other error while creating {}: {}, code: {}".format(
                instance.instanceType, e, DictQuery(e.response).get(['Error', 'Code'])))
            time.sleep(1.2**retcount)
            retcount += 1

        except Exception as e:
            logger.error("Other exception while creating {}: {}".format(
                instance.instanceType, e))
            time.sleep(1.2**retcount)
            retcount += 1

    if not ec2_inst:
        return None

    instance_id = ec2_inst['InstanceId']

    logger.info(
        "Waiting for instance {}/{} to be ready".format(instance.instanceType, instance_id))
    # wait for the instance
    try:
        waiter = ec2.get_waiter('instance_running')
        waiter.wait(InstanceIds=[instance_id], WaiterConfig={
            # wait for up to 30 minutes
            'Delay': 15,
            'MaxAttempts': 120
            })
    except Exception:
        logger.exception(
            'Waiter failed for {}/{}'.format(instance.instanceType, instance_id))

    # give 5 secs before trying ssh
    time.sleep(5)
    pkey = paramiko.RSAKey.from_private_key(
        StringIO(aws_get_parameter('/ssh_keys/{}'.format(ssh_keyname))))
    ssh = get_ssh_connection(ec2_inst, ssh_user, pkey, ssh_get_conn_timeout)
    if ssh is None:
        logger.error(
            "Couldn't open an ssh connection to {}, terminating instance".format(instance_id))
        ec2.terminate_instances(InstanceIds=[instance_id])
        return None

    # give some more time for the machine to be ready and to settle down
    time.sleep(60)

    # try stop all unnecessary services in order to provide a more reliable result
    for i in range(4):
        logger.info("Trying to stop services on {}, try #{}".format(instance_id, i))
        stdin, stdout, stderr = ssh.exec_command(stop_services_cmd)
        if stdout.channel.recv_exit_status() == 0:
            break
        time.sleep(5)
    else:
        # log, but don't fail
        logger.error("Couldn't stop services on {}: {}, {}".format(
            instance_id, stdout.read(), stderr.read()))

    results = []
    for name, bench_data in benchmarks_to_run.items():
        docker_img = bench_data['images'].get(instance.cpu_arch, None)
        if not docker_img:
            logger.error("Couldn't find docker image for {}/{}".format(name, instance.cpu_arch))
            continue
        # docker pull and wait some time
        for i in range(4):
            logger.info("Docker pull on {}, try #{}".format(instance_id, i))
            stdin, stdout, stderr = ssh.exec_command("docker pull {}; sync; sleep 10".format(docker_img))
            if stdout.channel.recv_exit_status() == 0:
                break
            time.sleep(5)
        else:
            logger.error("Couldn't pull docker image on {}: {}, {}".format(
                instance_id, stdout.read(), stderr.read()))

        if 'cpus' in bench_data and bench_data['cpus']:
            cpulist = bench_data['cpus']
        else:
            cpulist = range(1, instance.vcpu+1)
        for i in cpulist:
            dcmd = bench_data['cmd'].format(numcpu=i)
            cmd = 'docker run --network none --rm {} {}'.format(docker_img, dcmd)
            scores = []
            for it in range(bench_data.get('iterations', 3)):
                logger.info("Running on {}, command: {}, iter: #{}".format(instance_id, cmd, it))
                stdin, stdout, stderr = ssh.exec_command(cmd)
                ec = stdout.channel.recv_exit_status()
                stdo = stdout.read()
                stdrr = stderr.read()
                if ec == 0:
                    try:
                        scores.append(float(stdo))
                    except Exception:
                        logger.info(
                            "Couldn't parse output on {}, {}".format(instance_id, stdo))
                        scores.append(None)
                else:
                    logger.info("Non-zero exit code on {}, {}, {}, {}".format(instance_id, ec, stdo, stdrr))
            aggr_f = bench_data.get('score_aggregation', max)
            score = aggr_f(scores)
            results.append({'instanceType': instance.instanceType,
                            'benchmark_cpus': i, 'benchmark_score': score, 'benchmark_id': name,
                            'benchmark_name': bench_data.get('name'),
                            'benchmark_cmd': cmd, 'benchmark_program': bench_data.get('program'),
                            'date': datetime.now()})

    logger.info("Finished with instance {}, terminating".format(instance_id))
    ec2.terminate_instances(InstanceIds=[instance_id])

    return pd.DataFrame.from_dict(results)


def get_benchmarks_to_run(instance, perf_df, expire):
    my_benchmarks = copy.deepcopy(benchmarks)
    # filter the incoming perf data only to our instance type
    perf_df = perf_df[perf_df['instanceType'] == instance.instanceType][['instanceType', 'benchmark_id', 'date']].drop_duplicates()
    for idx, row in perf_df.iterrows():
        if (datetime.now() - row.date).seconds >= expire:
            # leave the benchmark if it's not yet expired ...
            continue
        # ... and drop, if it is
        my_benchmarks.pop(row.benchmark_id, None)

    return my_benchmarks


def get_ec2_performance(prices_df, perf_df=None, update=None, expire=None, **filter_opts):
    # drop spot instances
    prices_df = prices_df.drop(prices_df[prices_df.spot == True].index)
    # remove duplicate instances, so we'll have a list of all on-demand instances
    prices_df = prices_df.drop_duplicates(subset='instanceType')

    bench_args = []
    for instance in prices_df.itertuples():
        ami = aws_get_latest_ami(arch=instance.cpu_arch)
        if perf_df is not None and update:
            benchmarks_to_run = get_benchmarks_to_run(instance, perf_df, expire)
        else:
            benchmarks_to_run = benchmarks

        if not benchmarks_to_run:
            # leave this instance out if there is no benchmark to run
            continue
        if instance.instanceType not in (
                                        't3.xlarge',
                                        # 'a1.xlarge',
                                         #'m5d.xlarge'
                                         #, 'c5.xlarge'
                                         ):
            continue
        print(instance.instanceType, len(benchmarks_to_run))
        #continue
        ami = aws_get_latest_ami(arch=instance.cpu_arch)
        bench_args.append([ami, instance, benchmarks_to_run])
    if bench_args:
        pool = ThreadPool(32)
        results = pool.map(run_benchmarks, bench_args)
        return pd.concat(results, ignore_index=True, sort=False)
    else:
        return pd.DataFrame({})