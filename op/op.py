import kopf
import kubernetes
import requests
import os
import time
from prometheus_client.parser import text_string_to_metric_families

global start_time, flag

execution_context = os.environ.get("EXECUTION_CONTEXT", "DEV")
threshold = int(os.environ.get("THRESHOLD","600"))

avg_response_time = 0
flag = True

@kopf.on.create('unipi.gr', 'v1', 'thesis')
def create_fn(body, spec, logger, **kwargs):
    # Get info from Database object
    name = body['metadata']['name']
    namespace = body['metadata']['namespace']
    type = spec['type']

    # Make sure type is provided
    if not type:
        raise kopf.HandlerFatalError(f"Type must be set. Got {type}.")

    # Pod template
    pod = {'apiVersion': 'v1', 'metadata': {'name' : name, 'labels': {'app': 'db'}}}

    # Service template
    svc = {'apiVersion': 'v1', 'metadata': {'name' : name}, 'spec': { 'selector': {'app': 'db'}, 'type': 'NodePort'}}

    # Mongo Database
    if type == 'mongo':

        image = 'mongo'
        port = 27017
        pod['spec'] = { 'containers': [ { 'image': image, 'name': type } ]}
        svc['spec']['ports'] = [{ 'port': port, 'targetPort': port}]

        # Make the Pod and Service the children of the Database object
        kopf.adopt(pod, owner=body)
        kopf.adopt(svc, owner=body)

        # Object used to communicate with the API Server
        api = kubernetes.client.CoreV1Api()

        # Create Pod
        obj = api.create_namespaced_pod(namespace, pod)
        msg = f"Pod {obj.metadata.name} created"
        logger.info(msg)

        # Create Service
        obj = api.create_namespaced_service(namespace, svc)
        logger.info(f"NodePort Service {obj.metadata.name} created, exposing on port {obj.spec.ports[0].node_port}")

        # Update status
        msg = f"Pod and Service created by Database {name}"
        return {'message': msg}
    
    # Flask App 
    if type == 'flask':

        image = 'kostaschikis/pstesterws'
        port = 5000

        deployment = {
            'apiVersion': 'apps/v1',
            'kind': 'Deployment', 
            'metadata': {
                'name' : name + '-deployment', 
                'labels': {'app': name}
            },
            'spec': {
                'replicas': 1,
                'selector' : {
                    'matchLabels': {'app': name},
                },
                'template': {
                    'metadata': {
                        'labels' : {'app': name},
                    }
                }
            }
        }

        deployment['spec']['template']['spec'] = {
            'containers': [
                {
                    'name': name,
                    'image': image,
                    'env': [
                        {'name': 'MONGO_HOSTNAME', 'value': 'mongo'},
                        {'name': 'MONGO_PORT', 'value': '27017'},
                        {'name': 'EXECUTION_CONTEXT', 'value': 'K8S'},
                        {'name': 'METRICS_EXPOSING_METHOD', 'value': 'PULL'}
                    ],
                    'ports': [ {'containerPort': port} ]
                }
            ]
        }

        service = {
            'apiVersion': 'v1', 
            'metadata': 
                {'name' : name}, 
            'spec': {
                'selector': {'app': name},
                'ports': [
                    {'port': port, 'targetPort': port}
                ]
            }
        }

        if execution_context == "DEV":
            service['spec']['type'] = 'NodePort'

        # Make the Pod and Service the children of the Database object
        kopf.adopt(deployment, owner=body)
        kopf.adopt(service, owner=body)

        # Object used to communicate with the API Server
        apps_api = kubernetes.client.AppsV1Api()
        core_api = kubernetes.client.CoreV1Api()

        # Create Deployment
        obj = apps_api.create_namespaced_deployment(namespace, deployment)
        msg = f"Deployment {obj.metadata.name} created"
        logger.info(f"Deployment {obj.metadata.name} created")
        # kopf.info(obj.to_dict(), reason='Cluster Updated', message=msg)


        # Create Service
        obj = core_api.create_namespaced_service(namespace, service)
        msg = f"Cluster Service {obj.metadata.name} created, exposing on port {obj.spec.ports[0].port}"
        if execution_context == 'DEV':
            msg = f"Cluster Service {obj.metadata.name} created, exposing on NodePort {obj.spec.ports[0].node_port}"
        logger.info(msg)
        # kopf.info(obj.to_dict(), reason='Cluster Updated', message=msg)

        # Update status
        msg = f"Deployment and Service created by App {name}"
        return {'message': msg}


# Flask Timer
@kopf.timer('unipi.gr', 'v1', 'thesis', field='spec.type', value='flask', idle=5, interval=5.0)
def monitor_flask(logger,**kwargs):
    api = kubernetes.client.CoreV1Api();
    # Find the Service to get the port
    res = api.list_namespaced_service('uni-work', field_selector="metadata.name=flask")
    
    port = res.items[0].spec.ports[0].port
    flask_hostname = res.items[0].metadata.name

    if execution_context == "DEV":
        node = api.list_node()
        flask_hostname = node.items[0].status.addresses[0].address
        port = res.items[0].spec.ports[0].node_port

    # Request metrics
    metrics_url = "http://{0}:{1}/metrics".format(flask_hostname, port)
    metrics = requests.get(metrics_url)

    metrics_obj  = {}

    for family in text_string_to_metric_families(metrics.text):
        for sample in family.samples:
            metrics_obj[sample[0]] = sample[2]

    logger.info(metrics_obj)

    replicas = get_deployment_replicas(kubernetes.client.AppsV1Api(), 'uni-work', 'flask-deployment')
    
    response_time = metrics_obj.get('response_time')
    
    if (response_time > threshold):
        logger.warn(f'Response time has increased to {response_time}')
        replicas = replicas + 1
        update_deployment_replicas(kubernetes.client.AppsV1Api(), 'flask-deployment', replicas, 'uni-work')
        logger.info('Deployment replicas updated"')
        time.sleep(30)

    updated_replicas = get_deployment_replicas(kubernetes.client.AppsV1Api(), 'uni-work', 'flask-deployment')
    
    logger.info(f'Replicas Number: {updated_replicas}')



def update_deployment_replicas(api_instance, deployment_name, new_replicas, namespace):
    # Update Deployment replicas
    body = {
        "spec": {
            "replicas": new_replicas
        }
    }
    api_response = api_instance.patch_namespaced_deployment(
        name=deployment_name,
        namespace=namespace,
        body=body
    )
    print("Deployment replicas updated")

def get_deployment_replicas(api_instance, namespace, deployment):
    res = api_instance.read_namespaced_deployment(deployment, namespace)
    return res.status.available_replicas
    

@kopf.on.delete('unipi.gr', 'v1', 'thesis')
def delete(spec, meta, logger, **kwargs):
    if (spec['type'] == 'mongo'):
        msg = f"Database {meta['name']} and its Pod / Service children deleted"
        logger.info(msg)
    elif (spec['type'] == 'flask'):
        msg = f"Deployment {meta['name']} and its Service children deleted"
        logger.info(msg)
    return {'message': msg}