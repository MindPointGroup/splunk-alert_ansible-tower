#!/usr/bin/python

import sys, urllib2, json, tower_cli, os, datetime
import splunk.entity as entity
# Tower Connect
#
# This script is used as wrapper to connect to Ansible Tower API.

__author__ = "Keith Rhea"
__email__ = "keithr@mindpointgroup.com"
__version__ = "1.0"

#Securely retrieve Ansible Tower Credentials from Splunk REST API password endpoint
def getCredentials(sessionKey,realm):
   myapp = 'alert_ansible_tower'
   try:
      # list all credentials
      entities = entity.getEntities(['admin', 'passwords'], namespace=myapp,
                                    owner='nobody', sessionKey=sessionKey)
   except Exception, e:
      log("Could not get %s credentials from splunk. Error: %s"
                      % (myapp, str(e)))

   # return first set of credentials
   for i, c in entities.items():
        if c.get('realm')  == realm:
            return c['username'], c['clear_password']

   log("ERROR: No credentials have been found")

#Connect to Tower and authenticate using user/pass to receive auth token.
def tower_auth(hostname,username,password):
	try:
		req = urllib2.Request(
			url = 'https://' + hostname + '/api/v2/authtoken/',
			headers = {
				"Content-Type": "application/json"
			},
			data = json.dumps({
				"username": username,
				"password": password
			})
		)
		response = urllib2.urlopen(req)
		results = json.loads(response.read())
		token = results['token']
		return token
	except urllib2.URLError as error:
		log(error.reason)

def tower_launch(hostname,username,password,job_id,extra_vars):
	
	#Authenticate to Ansible Tower and receive Auth Token.
	token = tower_auth(hostname,username,password)
	
	#Attempt to Launch Ansible Tower Job Template
	try:
		req = urllib2.Request(
			url = 'https://' + hostname + '/api/v2/job_templates/' + job_id +'/launch/',
			headers = {
				"Content-Type": "application/json",
				"authorization": 'Token ' + token
			},
			data = json.dumps({
				"extra_vars": extra_vars
			})
		)
		response = urllib2.urlopen(req)
		results = json.loads(response.read())
		log("Job ID: " + str(results['job']) + " submitted successfully.")
	except urllib2.URLError as error:
		log(error.reason)
#Logging Function 
def log(settings):
    f = open(os.path.join(os.environ["SPLUNK_HOME"], "var", "log", "splunk", "tower_api.log"), "a")
    print >> f, str(datetime.datetime.now().isoformat()), settings 
    f.close()


def main(payload):
	#Retrieve session key from payload to authenticate to Splunk REST API for secure credential retrieval
        sessionKey = payload.get('session_key')
        
        #Retrieve Ansible Tower Hostname from Payload configuration
		hostname = payload['configuration'].get('hostname')
        
        #Retrieve Ansible Tower Job Template ID from Payload configuration
		job_id = payload['configuration'].get('job_id')
        
        #Retrieve realm  from Payload configuration
		realm = payload['configuration'].get('realm')

        #Retrieve Ansible Tower extra_vars Variable Name from Payload configuration
        var_name = payload['configuration'].get('var_name')

        #Retrieve Ansible Tower extra_vars Field to pull search value from Payload configuration
        var_field = payload['configuration'].get('var_field')

        #Retrieve Ansible Tower extra_vars value from Payload configuration
        var_value = payload['result'].get(var_field)
        
        #Assign extra_vars variable a value
        extra_vars = str(var_name) + ": " + str(var_value)
	
        #Retrive Ansible Tower Credentials from Splunk REST API
        username, password = getCredentials(sessionKey,realm)
        
        #Submit Ansible Tower Job
	tower_launch(hostname,username,password,job_id,extra_vars)



if __name__ == "__main__":
    # Check if script initiated with --execute
    if len(sys.argv) < 2 or sys.argv[1] != "--execute":
        print >> sys.stderr, "FATAL Unsupported execution mode (expected --execute flag)"
        sys.exit(1)
    else:
    	#Get Payload
    	payload = json.loads(sys.stdin.read())
    	log("Job Started")
        #Pass Pass Payload to main function
    	main(payload)
