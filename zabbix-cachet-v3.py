#!/usr/bin/env python3
"""
This script populated Cachet of Zabbix IT Services
"""
import sys
import os
import datetime
import json
import requests
import time
import threading
import logging
import yaml
import pytz
import traceback
from pyzabbix import ZabbixAPI, ZabbixAPIException
from operator import itemgetter
from enum import Enum
import urllib3


__author__ = "Artem Alexandrov <qk4l()tem4uk.ru>"
__license__ = """The MIT License (MIT)"""
__version__ = "1.3.7"

os.environ["REQUESTS_CA_BUNDLE"] = "/etc/ssl/certs/ca-certificates.crt"


class CachetComponentStatus(Enum):
    OPERATIONAL = 1
    PERFORMANCE_ISSUES = 2
    PARTIAL_OUTAGE = 3
    MAJOR_OUTAGE = 4
    UNKNOWN = 5


# Reference
# https://www.zabbix.com/documentation/7.2/en/manual/api/reference/service/object
class ZabbixServiceStatus(Enum):
    OK = -1
    NOT_CLASSIFIED = 0
    INFORMATION = 1
    WARNING = 2
    AVERAGE = 3
    HIGH = 4
    DISASTER = 5


class CachetIncidentStatus(Enum):
    REPORTED = 0
    INVESTIGATING = 1
    IDENTIFIED = 2
    WATCHING = 3
    FIXED = 4


def map_zabbix_status_to_cachet_status(zabbix_status):
    mapping = {
        ZabbixServiceStatus.OK: CachetComponentStatus.OPERATIONAL,
        ZabbixServiceStatus.NOT_CLASSIFIED: CachetComponentStatus.UNKNOWN,
        ZabbixServiceStatus.INFORMATION: CachetComponentStatus.OPERATIONAL,
        ZabbixServiceStatus.WARNING: CachetComponentStatus.PERFORMANCE_ISSUES,
        ZabbixServiceStatus.AVERAGE: CachetComponentStatus.PARTIAL_OUTAGE,
        ZabbixServiceStatus.HIGH: CachetComponentStatus.MAJOR_OUTAGE,
        ZabbixServiceStatus.DISASTER: CachetComponentStatus.MAJOR_OUTAGE,
    }
    return mapping.get(zabbix_status, CachetComponentStatus.UNKNOWN).value


def client_http_error(url, code, message):
    logging.error("ClientHttpError[%s, %s: %s]" % (url, code, message))


def cachetapiexception(message):
    logging.error(message)


def pyzabbix_safe(fail_result=False):
    def wrap(func):
        def wrapperd_f(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except (requests.ConnectionError, ZabbixAPIException) as e:
                logging.error("Zabbix Error: {}".format(e))
                return fail_result

        return wrapperd_f

    return wrap


class Zabbix:
    def __init__(self, server, user, password, verify=True):
        """
        Init zabbix class for further needs
        :param user: string
        :param password: string
        :return: pyzabbix object
        """
        self.server = server
        self.user = user
        self.password = password
        # Enable HTTP auth
        s = requests.Session()
        s.auth = (user, password)

        self.zapi = ZabbixAPI(server)
        self.zapi.session.verify = verify
        self.zapi.login(user, password)
        self.version = self.get_version()

    @pyzabbix_safe()
    def get_version(self):
        """
        Get Zabbix API version
        :return: str
        """
        version = self.zapi.apiinfo.version()
        return version

    @pyzabbix_safe({})
    def get_trigger(self, triggerid):
        """
        Get trigger information
        @param triggerid: string
        @return: dict of data
        """
        trigger = self.zapi.trigger.get(
            expandComment="true", expandDescription="true", triggerids=triggerid
        )
        return trigger[0]

    @pyzabbix_safe({})
    def get_event(self, triggerid):
        """
        Get event information based on triggerid
        @param triggerid: string
        @return: dict of data
        """
        zbx_event = self.zapi.event.get(
            select_acknowledges="extend",
            expandDescription="true",
            object=0,
            value=1,
            objectids=triggerid,
        )
        if len(zbx_event) >= 1:
            return zbx_event[-1]
        return zbx_event

    @pyzabbix_safe([])
    def get_itservices(self, root=None):
        """
        Return tree of Zabbix IT Services
        root (hidden)
           - service1 (Cachet componentgroup)
             - child_service1 (Cachet component)
             - child_service2 (Cachet component)
           - service2 (Cachet componentgroup)
             - child_service3 (Cachet component)
        :param root: Name of service that will be root of tree.
                    Actually it will not be present in return tree.
                    It's using just as a start point , string
        :return: Tree of Zabbix IT Services
        :rtype: list
        """
        if root:
            logging.debug(f"Obtained root service: 1")
            root_service = self.zapi.service.get(
                # selectDependencies='extend',
                output="extend",
                selectChildren="extend",
                selectProblemTags="extend",
                # selectParents='extend',
                filter={"name": root},
            )
            logging.debug(f"Obtained root service: 2")
            try:
                root_service = root_service[0]
                logging.debug(f"Obtained root service: {root_service}")
            except IndexError:
                logging.error('Can not find "{}" service in Zabbix'.format(root))
                sys.exit(1)
            service_ids = []
            for dependency in root_service["children"]:
                service_ids.append(dependency["serviceid"])
            services = self.zapi.service.get(
                # selectDependencies='extend',
                selectChildren="extend",
                selectProblemTags="extend",
                # selectParents='extend',
                serviceids=service_ids,
            )
        else:
            services = self.zapi.service.get(
                # selectDependencies='extend',
                selectChildren="extend",
                selectParents="extend",
                selectProblemTags="extend",
                output="extend",
            )
        if not services:
            logging.error('Can not find any child service for "{}"'.format(root))
            return []
        # Create a tree of services
        known_ids = []
        # At first proceed services with dependencies as groups
        service_tree = [i for i in services if i["children"]]
        for idx, service in enumerate(service_tree):
            child_services_ids = []
            for dependency in service["children"]:
                child_services_ids.append(dependency["serviceid"])
            child_services = self.zapi.service.get(
                # selectDependencies='extend',
                selectChildren="extend",
                # selectParents='extend',
                selectProblemTags="extend",
                serviceids=child_services_ids,
            )
            service_tree[idx]["children"] = child_services
            # Save ids to filter them later
            known_ids = known_ids + child_services_ids
            known_ids.append(service["serviceid"])
        # At proceed services without dependencies as singers
        singers_services = [i for i in services if i["serviceid"] not in known_ids]
        if singers_services:
            service_tree = service_tree + singers_services
        return service_tree


class Cachet:
    def __init__(self, server, token, verify=True):
        """
        Init Cachet class for further needs
        : param server: string
        :param token: string
        :return: object
        """
        self.server = server + "/api/"
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json; indent=4",
        }
        self.verify = verify
        self.version = self.get_version()

    def _http_post(self, url, params):
        """
        Make POST and return json response
        :param url: str
        :param params: dict
        :return: json
        """
        url = self.server + url
        payload = {"visible": True, "enabled": True, **params}
        logging.debug(
            "Sending to {url}: {payload}".format(
                url=url, payload=json.dumps(payload, indent=4, separators=(",", ": "))
            )
        )
        try:
            response = requests.request("POST", url, json=payload, headers=self.headers)
        except requests.exceptions.RequestException as err:
            raise client_http_error(url, response.status_code, err)

        if response.status_code not in [200, 201]:
            return client_http_error(url, response.status_code, response.text)

        try:
            r_json = json.loads(response.text)
        except ValueError:
            raise cachetapiexception("Unable to parse json: %s" % r.text)
        logging.debug(
            "Response Body: %s", json.dumps(r_json, indent=4, separators=(",", ": "))
        )
        return r_json

    def _http_get(self, url, params=None):
        """
        Helper for HTTP GET request
        :param: url: str
        :param: params:
        :return: json data
        """
        if params is None:
            params = {}
        url = self.server + url
        logging.debug(
            "Sending to {url}: {param}".format(
                url=url, param=json.dumps(params, indent=4, separators=(",", ": "))
            )
        )
        try:
            r = requests.get(
                url=url, headers=self.headers, params=params, verify=self.verify
            )
        except requests.exceptions.RequestException as e:
            raise client_http_error(url, None, e)
        # r.raise_for_status()
        if r.status_code != 200:
            print("response text =======================", r)
            sys.exit(1)
            return client_http_error(url, r.status_code, json.loads(r.text)["errors"])
        try:
            r_json = json.loads(r.text)
        except ValueError:
            raise cachetapiexception("Unable to parse json: %s" % r.text)
        logging.debug(
            "Response Body: %s", json.dumps(r_json, indent=4, separators=(",", ": "))
        )
        return r_json

    def _http_put(self, url, params):
        """
        Make PUT and return json response
        :param url: str
        :param params: dict
        :return: json
        """
        url = self.server + url
        logging.debug(
            "Sending to {url}: {param}".format(
                url=url, param=json.dumps(params, indent=4, separators=(",", ": "))
            )
        )
        try:
            r = requests.put(
                url=url, json=params, headers=self.headers, verify=self.verify
            )
        except requests.exceptions.RequestException as e:
            raise client_http_error(url, None, e)
        # r.raise_for_status()
        if r.status_code != 200:
            return client_http_error(url, r.status_code, r.text)
        try:
            r_json = json.loads(r.text)
        except ValueError:
            raise cachetapiexception("Unable to parse json: %s" % r.text)
        logging.debug(
            "Response Body: %s", json.dumps(r_json, indent=4, separators=(",", ": "))
        )
        return r_json

    def get_version(self):
        """
        Get Cachet version for logging
        :return: str
        """
        url = "version"
        data = self._http_get(url)
        return data["data"]

    def get_component(self, id):
        """
        Get component params based its id
        @param id: string
        @return: dict
        """
        url = "components/" + str(id)
        data = self._http_get(url)
        return data

    def get_components(self, name=None):
        """
        Get all registered components or return a component details if name specified
        Please note, it name was not defined method returns only last page of data
        :param name: Name of component to search
        :type name: str
        :return: Data =)
        :rtype: dict or list
        """
        url = "components"
        if name:
            return self.find_component_by_name(name, url)

        data = self._http_get(url)
        return data

    def find_component_by_name(self, name, url):
        """
        Find a component by name from a paginated API.

        Args:
            name (str): The name of the group to search for.
            url (str): The base URL for fetching paginated data.

        Returns:
            dict: The group data if found, otherwise a default "not found" response.
        """
        page = 1  # Start with the first page

        while True:
            # Fetch the current page data
            response = self._http_get(url, params={"page": page, "include": "group"})

            data = response.get("data", [])
            meta = response.get("meta", {})
            for component in data:
                if component.get("attributes").get("name") == name:
                    return component  # Return the group if found

            if not data or "current_page" not in meta:
                break  # Exit loop if no more pages

            page += 1

        return {"id": 0, "name": "Does not exist"}

    def new_components(self, name, **kwargs):
        """
        Create new components
        @param name: string
        @param kwargs: various additional values =)
        @return: dict of data
        """
        # Get values for new component
        params = {
            "name": name,
            "link": "",
            "description": "",
            "status": 1,
            "component_group_id": 0,
        }
        params.update(kwargs)
        for i in ("link", "description"):
            if str(params[i]).strip() == "":
                params.pop(i)

        component = self.get_components(name)
        if isinstance(component, list):
            for i in component:
                if i["component_group_id"] == params["component_group_id"]:
                    return i
        elif isinstance(component, dict):
            relationships = component.get("relationships")
            group_id = 0
            if relationships:
                group = relationships.get("group")
                if group:
                    data = group.get("data")
                    if data:
                        group_id = data.get("id")
            if not component["id"] == 0 and group_id == params["component_group_id"]:
                return component

        # Create component if it does not exist or exist in other group
        url = "components"
        logging.debug("Creating Cachet component {name}...".format(name=params["name"]))
        params["componentGroupId"] = params["component_group_id"]
        data = self._http_post(url, params)
        return data["data"]

    def upd_components(self, id, **kwargs):
        """
        Update component
        @param id: string
        @param kwargs: various additional values =)
        @return: boolean
        """
        url = "components/" + str(id)
        params = self.get_component(id)["data"]
        params.update(kwargs)
        data = self._http_put(url, params)
        if data:
            logging.info(
                "Component {name} (id={id}) was updated. Status - {status}".format(
                    name=data["data"]["attributes"]["name"],
                    id=id,
                    status=data["data"]["attributes"]["status"]["human"],
                )
            )
        return data

    def get_components_gr(self, name=None):
        """
        Get all registered components group or return a component group details if name specified
        Please note, it name was not defined method returns only last page of data
        @param name: string
        @return: dict of data
        """
        url = "component-groups"
        data = self._http_get(url)
        if name:
            return self.find_group_by_name(name, url)
        return data

    def find_group_by_name(self, name, url):
        """
        Find a group by name from a paginated API.

        Args:
            name (str): The name of the group to search for.
            url (str): The base URL for fetching paginated data.

        Returns:
            dict: The group data if found, otherwise a default "not found" response.
        """
        page = 1  # Start with the first page

        while True:
            response = self._http_get(url, params={"page": page})

            data = response.get("data", [])
            meta = response.get("meta", {})

            for group in data:
                if group.get("attributes").get("name") == name:
                    return group

            if not data or "current_page" not in meta:
                break  # Exit loop if no more pages

            page += 1

        return {"id": 0, "name": "Does not exist"}

    def new_components_gr(self, name):
        """
        Create new components group
        @param name: string
        @return: dict of data
        """
        # Check if component's group already exists
        components_gr_id = self.get_components_gr(name)
        if components_gr_id["id"] == 0:
            url = "component-groups"
            params = {"name": name, "collapsed": 2}
            logging.debug("Creating Component Group {}...".format(params["name"]))
            data = self._http_post(url, params)
            if data is not None and "data" in data:
                logging.info(
                    "Component Group {} was created ({})".format(
                        params["name"], data["data"]["id"]
                    )
                )

            return data["data"]

        return components_gr_id

    def get_unresolved_incident(self, component_id):
        """
        Get last incident for component_id
        @param component_id: string
        @return: dict of data
        """
        url = "incidents"
        page = 1  # Start with the first page

        while True:
            # Fetch the current page data
            response = self._http_get(url, params={"page": page})

            data = response.get("data", [])
            meta = response.get("meta", {})
            for incident in data:
                if (
                    incident.get("attributes").get("component_id") == int(component_id)
                    and "__Resolved__" not in incident["attributes"]["message"]
                ):
                    return incident

            if not data or meta.get("to") is None:
                break

            page += 1

        return {
            "id": "0",
            "attributes": {
                "id": 0,
                "name": "Does not exist",
                "status": {"human": "Does not exist", "value": -1},
            },
        }

    def new_incidents(self, **kwargs):
        """
        Create a new incident.
        @param kwargs: various additional values =)
                        name, message, status,
                        component_id, component_status
        @return: dict of data
        """
        params = {"visible": 1, "notify": "true"}
        url = "incidents"
        params.update(kwargs)

        response = self._http_post(url, params)
        logging.info(
            "Incident {name} (id={incident_id}) was created for component id {component_id}.".format(
                name=params["name"],
                incident_id=response["data"].get("id"),
                component_id=params["component_id"],
            )
        )

        if "component_status" in params and params["component_id"] is not None:
            self.upd_components(
                params["component_id"], status=params["component_status"]
            )
        return response["data"]

    def upd_incident(self, id, **kwargs):
        """
        Update incident
        @param id: string
        @param kwargs: various additional values =)
                message, status,
                component_status
        @return: boolean
        """
        url = "incidents/" + str(id)
        params = kwargs
        response = self._http_put(url, params)
        logging.info(
            "Incident ID {id} was updated. Status - {status}.".format(
                id=id, status=response["data"]["attributes"]["status"]["human"]
            )
        )

        if "component_status" in params and params["component_id"] is not None:
            self.upd_components(
                params["component_id"], status=params["component_status"]
            )

        return response


def triggers_watcher(service_map):
    """
    Check zabbix triggers and update Cachet components
    Zabbix Priority:
        0 - (default) not classified;
        1 - information;
        2 - warning;
        3 - average;
        4 - high;
        5 - disaster.
    Cachet Incident Statuses:
        0 - Scheduled - This status is used for a scheduled status.
        1 - Investigating - You have reports of a problem and you're currently looking into them.
        2 - Identified - You've found the issue and you're working on a fix.
        3 - Watching - You've since deployed a fix and you're currently watching the situation.
        4 - Fixed
    @param service_map: list of tuples
    @return: boolean
    """
    for i in service_map:
        inc_status = CachetIncidentStatus.INVESTIGATING.value
        comp_status = CachetComponentStatus.OPERATIONAL.value
        # inc_name = ''
        inc_msg = ""

        logging.debug("Object {}".format(i))

        if "triggerid" in i:
            trigger = zapi.get_trigger(i["triggerid"])
            # Check if Zabbix return trigger
            if "value" not in trigger:
                logging.error("Cannot get value for trigger {}".format(i["triggerid"]))
                continue
            # Check if incident already registered
            # Trigger non Active
            if str(trigger["value"]) == "0":
                component = cachet.get_component(i["component_id"])
                component_status = (
                    component.get("data").get("attributes").get("status").get("value")
                )

                if str(component_status) == "1":
                    continue

                last_inc = cachet.get_unresolved_incident(i["component_id"])
                if str(last_inc["id"]) != "0":
                    if resolving_tmpl:
                        inc_msg = (
                            resolving_tmpl.format(
                                time=datetime.datetime.now(tz=tz).strftime(
                                    "%b %d, %H:%M"
                                ),
                            )
                            + cachet.get_unresolved_incident(i["component_id"])[
                                "attributes"
                            ]["message"]
                        )
                    else:
                        inc_msg = cachet.get_unresolved_incident(i["component_id"])[
                            "attributes"
                        ]["message"]
                    cachet.upd_incident(
                        last_inc["id"],
                        status=4,
                        component_id=i["component_id"],
                        component_status=1,
                        message=inc_msg,
                    )
                # Incident does not exist. Just change component status
                else:
                    cachet.upd_components(i["component_id"], status=1)
                continue
            if trigger["value"] == "1":
                zbx_event = zapi.get_event(i["triggerid"])
                inc_name = trigger["description"]
                if not zbx_event:
                    logging.warning(
                        "Failed to get zabbix event for trigger {}".format(
                            i["triggerid"]
                        )
                    )
                    # Mock zbx_event for further usage
                    zbx_event = {
                        "acknowledged": "0",
                    }
                if zbx_event.get("acknowledged", "0") == "1":
                    inc_status = CachetIncidentStatus.IDENTIFIED.value
                    for msg in zbx_event["acknowledges"]:
                        # TODO: Add timezone?
                        #       Move format to config file
                        author = msg.get("name", "") + " " + msg.get("surname", "")
                        ack_time = datetime.datetime.fromtimestamp(
                            int(msg["clock"]), tz=tz
                        ).strftime("%b %d, %H:%M")
                        ack_msg = acknowledgement_tmpl.format(
                            message=msg["message"], ack_time=ack_time, author=author
                        )
                        if ack_msg not in inc_msg:
                            inc_msg = ack_msg + inc_msg
                else:
                    inc_status = CachetIncidentStatus.INVESTIGATING.value
                if int(trigger["priority"]) >= ZabbixServiceStatus.HIGH.value:
                    comp_status = CachetComponentStatus.MAJOR_OUTAGE.value
                elif int(trigger["priority"]) == ZabbixServiceStatus.AVERAGE.value:
                    comp_status = CachetIncidentStatus.PARTIAL_OUTAGE.value
                else:
                    comp_status = CachetIncidentStatus.PERFORMANCE_ISSUES.value

                if not inc_msg and investigating_tmpl:
                    if zbx_event:
                        zbx_event_clock = int(zbx_event.get("clock"))
                        zbx_event_time = datetime.datetime.fromtimestamp(
                            zbx_event_clock, tz=tz
                        ).strftime("%b %d, %H:%M")
                    else:
                        zbx_event_time = ""
                    inc_msg = investigating_tmpl.format(
                        group=i.get("group_name", ""),
                        component=i.get("component_name", ""),
                        time=zbx_event_time,
                        trigger_description=trigger.get("comments", ""),
                        trigger_name=trigger.get("description", ""),
                    )

                if not inc_msg and trigger.get("comments"):
                    inc_msg = trigger.get("comments")
                elif not inc_msg:
                    inc_msg = trigger.get("description")

                if "group_name" in i:
                    inc_name = i.get("group_name") + " | " + inc_name

                last_inc = cachet.get_unresolved_incident(i["component_id"])
                # Incident not registered
                if last_inc["attributes"]["status"]["value"] == -1:
                    cachet.new_incidents(
                        name=inc_name,
                        message=inc_msg,
                        status=inc_status,
                        component_id=i["component_id"],
                        component_status=comp_status,
                    )

                # Incident already registered
                elif last_inc["attributes"]["status"]["value"] not in (-1, 4):
                    # Only incident message can change. So check if this have happened
                    if last_inc["attributes"]["message"].strip() != inc_msg.strip():
                        cachet.upd_incident(
                            last_inc["id"],
                            message=inc_msg,
                            status=inc_status,
                            component_status=comp_status,
                        )

        else:
            # TODO: ServiceID
            # inc_msg = 'TODO: ServiceID'
            continue

    return True


def triggers_watcher_worker(service_map, interval, event):
    """
    Worker for triggers_watcher. Run it continuously with specific interval
    @param service_map: list of tuples
    @param interval: interval in seconds
    @param event: treading.Event object
    @return:
    """
    logging.info("Start trigger watcher....")
    while not event.is_set():
        logging.debug("check Zabbix triggers")
        # Do not run if Zabbix is not available
        if zapi.get_version():
            try:
                triggers_watcher(service_map)
            except Exception as e:
                logging.error(
                    "triggers_watcher() raised an Exception. Something gone wrong"
                )
                logging.error(e, exc_info=True)
        else:
            logging.error("Zabbix is not available. Skip checking...")
        time.sleep(interval)
    logging.info("end trigger watcher")


def init_cachet(services):
    """
    Init Cachet by syncing Zabbix service to it
    Also creates mapping between Cachet components and Zabbix IT services
    @param services: list
    @return: list of tuples
    """

    data = []
    for zbx_service in services:
        if zbx_service.get("children"):
            data.extend(process_zbx_service_with_children(zbx_service))
        else:
            data.extend(process_zbx_service_without_children(zbx_service))

    return data


def process_zbx_service_with_children(zbx_service):
    """
    Process Zabbix service with children and create Cachet components for each dependency.
    @param zbx_service: dict
    @return: list of tuples
    """
    data = []
    group = cachet.new_components_gr(zbx_service["name"])

    for dependency in zbx_service["children"]:
        logging.debug("dependency: %s", dependency)
        dependency["status"] = map_zabbix_status_to_cachet_status(
            dependency.get("status")
        )
        zxb2cachet_i = {}
        if dependency.get("problem_tags"):
            zxb2cachet_i = process_dependency_with_problem_tags(dependency, group)
        elif dependency.get("triggerid"):
            zxb2cachet_i = process_dependency_with_triggerid(dependency, group)
        else:
            zxb2cachet_i = process_dependency_without_trigger(dependency, group)

        logging.debug("group {}".format(group))
        zxb2cachet_i.update(
            {
                "group_id": group["id"],
                "group_name": group.get("attributes").get("name"),
            }
        )
        data.append(zxb2cachet_i)

    return data


def process_zbx_service_without_children(zbx_service):
    """
    Process Zabbix service without children and create Cachet components if a trigger exists.
    @param zbx_service: dict
    @return: list of tuples
    """
    data = []
    if "triggerid" in zbx_service:
        if int(zbx_service["triggerid"]) == 0:
            logging.error(
                "Zabbix Service with service id = {} does not have trigger or child service".format(
                    zbx_service["serviceid"]
                )
            )
            return data

        trigger = zapi.get_trigger(zbx_service["triggerid"])
        if not trigger:
            logging.error(
                "Failed to get trigger {} from Zabbix".format(zbx_service["triggerid"])
            )
            return data

        component = cachet.new_components(
            zbx_service["name"],
            link=trigger["url"],
            description=trigger["description"],
        )
        zxb2cachet_i = {
            "triggerid": zbx_service["triggerid"],
            "component_id": component["id"],
            "component_name": component["name"],
        }
        data.append(zxb2cachet_i)
    else:
        logging.error(
            "Service {} does not have associated triggerid, adjust Zabbix -> SLA Configuration".format(
                zbx_service["name"]
            )
        )
    return data


def process_dependency_with_problem_tags(dependency, group):
    """
    Process a dependency with problem tags and create Cachet components.
    @param dependency: dict
    @param group: dict
    @return: dict
    """
    for t in dependency.get("problem_tags"):
        if t.get("value"):
            trigger_id = str(t.get("value")).split(":")
            trigger = zapi.get_trigger(trigger_id[0])
            if not trigger:
                logging.error(
                    "Failed to get trigger {} from Zabbix".format(
                        dependency["triggerid"]
                    )
                )
                continue

            component = cachet.new_components(
                dependency["name"],
                component_group_id=group["id"],
                link=trigger["url"],
                status=dependency["status"],
                description=trigger["description"],
            )
            logging.debug("Created component {}".format(component))

            return {
                "triggerid": trigger_id,
                "component_id": component["id"],
                "component_name": component.get("attributes").get("name"),
            }
    return {}


def process_dependency_with_triggerid(dependency, group):
    """
    Process a dependency with triggerid and create Cachet components.
    @param dependency: dict
    @param group: dict
    @return: dict
    """
    trigger = zapi.get_trigger(dependency["triggerid"])
    if not trigger:
        logging.error(
            "Failed to get trigger {} from Zabbix".format(dependency["triggerid"])
        )
        return {}

    component = cachet.new_components(
        dependency["name"],
        component_group_id=group["id"],
        link=trigger["url"],
        status=dependency["status"],
        description=trigger["description"],
    )
    logging.debug("Created component {}".format(component))
    return {
        "triggerid": dependency["triggerid"],
        "component_id": component["id"],
        "component_name": component.get("attributes").get("name"),
    }


def process_dependency_without_trigger(dependency, group):
    """
    Process a dependency without trigger and create Cachet components.
    @param dependency: dict
    @param group: dict
    @return: dict
    """
    component = cachet.new_components(
        dependency["name"],
        component_group_id=group["id"],
        status=dependency["status"],
    )
    return {
        "serviceid": dependency["serviceid"],
        "component_id": component["id"],
        "component_name": component["name"],
    }


def read_config(config_f):
    """
    Read config file
    @param config_f: strung
    @return: dict of data
    """
    try:
        return yaml.safe_load(open(config_f, "r"))
    except (yaml.error.MarkedYAMLError, IOError) as e:
        logging.error("Failed to parse config file {}: {}".format(config_f, e))
    return None


if __name__ == "__main__":

    if os.getenv("CONFIG_FILE") is not None:
        CONFIG_F = os.environ["CONFIG_FILE"]
    else:
        CONFIG_F = os.path.dirname(os.path.realpath(__file__)) + "/config.yml"
    config = read_config(CONFIG_F)
    if not config:
        sys.exit(1)
    ZABBIX = config["zabbix"]
    CACHET = config["cachet"]
    SETTINGS = config["settings"]

    if SETTINGS.get("time_zone"):
        tz = pytz.timezone(SETTINGS["time_zone"])
    else:
        tz = None

    # Templates for incident displaying
    acknowledgement_tmpl_d = "{message}\n\n###### {ack_time} by {author}\n\n______\n"
    templates = config.get("templates")
    if templates:
        acknowledgement_tmpl = templates.get("acknowledgement", acknowledgement_tmpl_d)
        investigating_tmpl = templates.get("investigating", "")
        resolving_tmpl = templates.get("resolving", "")
    else:
        acknowledgement_tmpl = acknowledgement_tmpl_d

    exit_status = 0
    # Set Logging
    log_level = logging.getLevelName(SETTINGS["log_level"])
    log_level_requests = logging.getLevelName(SETTINGS["log_level_requests"])
    logging.basicConfig(
        format="%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d:%H:%M:%S",
        level=log_level,
    )
    logging.getLogger("requests").setLevel(log_level_requests)
    logging.info(
        "Zabbix Cachet v.{} started (config: {})".format(__version__, CONFIG_F)
    )
    inc_update_t = threading.Thread()
    event = threading.Event()
    try:
        if ZABBIX["https-verify"] is False:
            urllib3.disable_warnings()

        zapi = Zabbix(
            ZABBIX["server"], ZABBIX["user"], ZABBIX["pass"], ZABBIX["https-verify"]
        )
        cachet = Cachet(CACHET["server"], CACHET["token"], CACHET["https-verify"])
        logging.info(
            "Zabbix ver: {}. Cachet ver: {}".format(zapi.version, cachet.version)
        )
        zbxtr2cachet = ""
        while True:
            logging.debug("Getting list of Zabbix IT Services ...")
            itservices = zapi.get_itservices(SETTINGS["root_service"])
            logging.debug("Zabbix IT Services: {}".format(itservices))
            # Create Cachet components and components groups
            logging.debug("Syncing Zabbix with Cachet...")
            zbxtr2cachet_new = init_cachet(itservices)
            if not zbxtr2cachet_new:
                logging.error(
                    "Sorry, can not create Zabbix <> Cachet mapping for you. Please check above errors"
                )
                # Exit if it's a initial run
                if not zbxtr2cachet:
                    sys.exit(1)
                else:
                    zbxtr2cachet_new = zbxtr2cachet
            else:
                logging.info(
                    "Successfully synced Cachet components with Zabbix Services"
                )
            # Restart triggers_watcher_worker
            if zbxtr2cachet != zbxtr2cachet_new:
                zbxtr2cachet = zbxtr2cachet_new
                logging.info("Restart triggers_watcher worker")
                logging.debug("List of watching triggers {}".format(str(zbxtr2cachet)))
                event.set()
                # Wait until tread die
                while inc_update_t.is_alive():
                    time.sleep(1)
                event.clear()
                inc_update_t = threading.Thread(
                    name="Trigger Watcher",
                    target=triggers_watcher_worker,
                    args=(zbxtr2cachet, SETTINGS["update_inc_interval"], event),
                )
                inc_update_t.daemon = True
                inc_update_t.start()
            time.sleep(SETTINGS["update_comp_interval"])

    except KeyboardInterrupt:
        event.set()
        logging.info("Shutdown requested. See you.")
    except Exception as e:
        logging.error(e)
        logging.error(
            "@@@@ Thread Exception: {}, {}, {}".format(
                e, e.with_traceback, traceback.print_exc()
            )
        )

        exit_status = 1
    sys.exit(exit_status)
