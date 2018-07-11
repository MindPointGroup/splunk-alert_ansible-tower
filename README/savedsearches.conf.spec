# Tower API alert action settings

action.tower_api = [0|1]
* Enable tower_api action

action.tower_api.param.hostname = <string>
* Ansible Tower Host to send the HTTP POST request to. Must be accessible from the Splunk server.

action.tower_api.param.job_id = <string>
* Ansible Tower Job Template ID to send the HTTP POST request to.

action.tower_api.param.var_name = <string>
* The extra variable name that will be passed to Ansible Tower.

action.tower_api.param.var_field = <string>
* The field/column name from the alert query search results to be used as the value for extra variable.

