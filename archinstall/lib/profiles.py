import os, urllib.request, urllib.parse, ssl, json, re
import importlib.util, sys, glob, hashlib
from collections import OrderedDict
from .general import multisplit, sys_command, log
from .exceptions import *
from .networking import *
from .output import log, LOG_LEVELS
from .storage import storage

def grab_url_data(path):
	safe_path = path[:path.find(':')+1]+''.join([item if item in ('/', '?', '=', '&') else urllib.parse.quote(item) for item in multisplit(path[path.find(':')+1:], ('/', '?', '=', '&'))])
	ssl_context = ssl.create_default_context()
	ssl_context.check_hostname = False
	ssl_context.verify_mode=ssl.CERT_NONE
	response = urllib.request.urlopen(safe_path, context=ssl_context)
	return response.read()

def list_profiles(filter_irrelevant_macs=True):
	# TODO: Grab from github page as well, not just local static files
	if filter_irrelevant_macs:
		local_macs = list_interfaces()

	cache = {}
	# Grab all local profiles found in PROFILE_PATH
	for PATH_ITEM in storage['PROFILE_PATH']:
		for root, folders, files in os.walk(os.path.abspath(os.path.expanduser(PATH_ITEM))):
			for file in files:
				if os.path.splitext(file)[1] == '.py':
					tailored = False
					if len(mac := re.findall('(([a-zA-z0-9]{2}[-:]){5}([a-zA-z0-9]{2}))', file)):
						if filter_irrelevant_macs and mac[0][0].lower() not in local_macs:
							continue
						tailored = True

					description = ''
					with open(os.path.join(root, file), 'r') as fh:
						first_line = fh.readline()
						if first_line[0] == '#':
							description = first_line[1:].strip()

					cache[file[:-3]] = {'path' : os.path.join(root, file), 'description' : description, 'tailored' : tailored}
			break

	# Grab profiles from upstream URL
	if storage['PROFILE_DB']:
		profiles_url = os.path.join(storage["UPSTREAM_URL"], storage['PROFILE_DB'])
		try:
			profile_list = json.loads(grab_url_data(profiles_url))
		except urllib.error.HTTPError as err:
			print(f'Error: Listing profiles on URL "{profiles_url}" resulted in:', err)
			return cache
		except:
			print(f'Error: Could not decode "{profiles_url}" result as JSON:', err)
			return cache
		
		for profile in profile_list:
			if os.path.splitext(profile)[1] == '.py':
				tailored = False
				if len(mac := re.findall('(([a-zA-z0-9]{2}[-:]){5}([a-zA-z0-9]{2}))', profile)):
					if filter_irrelevant_macs and mac[0][0].lower() not in local_macs:
						continue
					tailored = True

				cache[profile[:-3]] = {'path' : os.path.join(storage["UPSTREAM_URL"], profile), 'description' : profile_list[profile], 'tailored' : tailored}

	return cache

class Script():
	def __init__(self, profile, installer=None):
		# profile: https://hvornum.se/something.py
		# profile: desktop
		# profile: /path/to/profile.py
		self.profile = profile
		self.installer = installer
		self.converted_path = None
		self.spec = None
		self.namespace = os.path.splitext(os.path.basename(self.path))[0]

	def __enter__(self, *args, **kwargs):
		self.execute()
		return sys.modules[self.namespace]

	def __exit__(self, *args, **kwargs):
		# TODO: https://stackoverflow.com/questions/28157929/how-to-safely-handle-an-exception-inside-a-context-manager
		if len(args) >= 2 and args[1]:
			raise args[1]

	def localize_path(self, profile_path):
		if (url := urllib.parse.urlparse(profile_path)).scheme and url.scheme in ('https', 'http'):
			if not self.converted_path:
				self.converted_path = f"/tmp/{os.path.basename(self.profile).replace('.py', '')}_{hashlib.md5(os.urandom(12)).hexdigest()}.py"

				with open(self.converted_path, "w") as temp_file:
					temp_file.write(urllib.request.urlopen(url.geturl()).read().decode('utf-8'))

			return self.converted_path
		else:
			return profile_path

	@property
	def path(self):
		parsed_url = urllib.parse.urlparse(self.profile)

		# The Profile was not a direct match on a remote URL
		if not parsed_url.scheme:
			# Try to locate all local or known URL's
			examples = list_profiles()

			if f"{self.profile}" in examples:
				return self.localize_path(examples[self.profile]['path'])
			# TODO: Redundant, the below block shouldnt be needed as profiles are stripped of their .py, but just in case for now:
			elif f"{self.profile}.py" in examples:
				return self.localize_path(examples[f"{self.profile}.py"]['path'])

			# Path was not found in any known examples, check if it's an abolute path
			if os.path.isfile(self.profile):
				return os.path.basename(self.profile)

			raise ProfileNotFound(f"File {self.profile} does not exist in {examples}")
		elif parsed_url.scheme in ('https', 'http'):
			return self.localize_path(self.profile)
		else:
			raise ProfileNotFound(f"Cannot handle scheme {parsed_url.scheme}")

	def load_instructions(self, namespace=None):
		if namespace:
			self.namespace = namespace

		self.spec = importlib.util.spec_from_file_location(self.namespace, self.path)
		imported = importlib.util.module_from_spec(self.spec)
		sys.modules[self.namespace] = imported
		
		return imported

	def execute(self):
		if not self.namespace in sys.modules or self.spec is None:
			self.load_instructions()

		__builtins__['installation'] = self.installer # TODO: Replace this with a import archinstall.session instead
		self.spec.loader.exec_module(sys.modules[self.namespace])

		return sys.modules[self.namespace]

class Profile(Script):
	def __init__(self, installer, path, args={}):
		super(Profile, self).__init__(path, installer)
		self._cache = None

	def __dump__(self, *args, **kwargs):
		return {'path' : self.path}

	def __repr__(self, *args, **kwargs):
		return f'Profile({self.path})'

	def install(self):
		return self.execute()

class Application(Profile):
	def __repr__(self, *args, **kwargs):
		return f'Application({self._path} <"{self.path}">)'

	@property
	def path(self, *args, **kwargs):
		if os.path.isfile(f'{self._path}'):
			return os.path.abspath(f'{self._path}')

		for path in ['./applications', './profiles/applications', '/etc/archinstall/applications', '/etc/archinstall/profiles/applications', os.path.abspath(f'{os.path.dirname(__file__)}/../profiles/applications')]:
			if os.path.isfile(f'{path}/{self._path}.py'):
				return os.path.abspath(f'{path}/{self._path}.py')

		try:
			if (cache := grab_url_data(f'{storage["UPSTREAM_URL"]}/applications/{self._path}.py')):
				self._cache = cache
				return f'{storage["UPSTREAM_URL"]}/applications/{self._path}.py'
		except urllib.error.HTTPError:
			pass

		return None