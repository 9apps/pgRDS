# Copyright (C) 2011, 2012 9apps B.V.
# 
# This file is part of Redis for AWS.
# 
# Redis for AWS is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# Redis for AWS is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with Redis for AWS. If not, see <http://www.gnu.org/licenses/>.

import os, sys, re, subprocess
import json, urllib2

from boto.s3.connection import S3Connection
from boto.s3.connection import Location
from boto.exception import S3CreateError

from boto.ec2.connection import EC2Connection
from boto.ec2.regioninfo import RegionInfo

from route53 import Route53Zone

try:
	url = "http://169.254.169.254/latest/"

	userdata = json.load(urllib2.urlopen(url + "user-data"))
	instance_id = urllib2.urlopen(url + "meta-data/instance-id").read()
	hostname = urllib2.urlopen(url + "meta-data/public-hostname/").read()

	zone = urllib2.urlopen(url + "meta-data/placement/availability-zone").read()
	region = zone[:-1]

	zone_name = os.environ['HOSTED_ZONE_NAME']
	zone_id = os.environ['HOSTED_ZONE_ID']
except Exception as e:
	exit( "We couldn't get user-data or other meta-data...")

pg_dir = '/var/lib/postgresql/9.1/'
pg_conf = '/etc/postgresql/9.1/main/postgresql.conf'

# we are going to work with local files, we need our path
path = os.path.dirname(os.path.abspath(__file__))

def create_device(device='/dev/sdf', size=10):
	# if we have the device just don't do anything anymore
	mapping = ec2.get_instance_attribute(instance_id, 'blockDeviceMapping')
	try:
		volume_id = mapping['blockDeviceMapping'][device].volume_id
	except:
		volume = ec2.create_volume(size, zone)

		# nicely wait until the volume is available
		while volume.volume_state() != "available":
			volume.update()

		volume.attach(instance_id, device)
		volume_id = volume.id

		# we can't continue without a properly attached device
		os.system("while [ ! -b {0} ] ; do /bin/true ; done".format(device))

		# make sure the volume is deleted upon termination
		# should also protect from disaster like loosing an instance
		# (it doesn't work with boto, so we do it 'outside')
		os.system("/usr/bin/ec2-modify-instance-attribute --block-device-mapping \"{0}=:true\" {1} --region {2}".format(device, instance_id, region))

def create_mount(dev='/dev/sdf', name='main'):
	mount = pg_dir + name

	if os.path.ismount(mount) == False:
		# it is probably new, so try to make an XFS filesystem
		os.system("/sbin/mkfs.xfs {0}".format(dev))

		# mount, but first wait until the device is ready
		os.system("sudo -u postgres /bin/mkdir -p {0}".format(mount))
		os.system("/bin/mount -t xfs -o defaults {0} {1}".format(dev, mount))
	else:
		# or grow (if necessary)
		os.system("/usr/sbin/xfs_growfs {0}".format(mount))

	os.system("chown -R postgres.postgres {0}".format(mount))


def set_cron(archive="db-fashiolista-com"):
	cron = "{0}/cron.d/postgres.cron".format(path)
	os.system("/usr/bin/crontab -u postgres {0}".format(cron))

def add_monitor(device="/dev/sdf", name="main"):
	f = open( "{0}/etc/monit/{1}".format(path, name), "w")
	f.write("  check filesystem {0} with path {1}".format(name, device))
	f.write("	if failed permission 660 then alert")
	f.write("	if failed uid root then alert")
	f.write("	if failed gid disk then alert")
	f.write("	if space usage > 80% for 5 times within 15 cycles then alert")
	f.close()

def monitor():
	os.system("/usr/sbin/monit reload")

if __name__ == '__main__':
	region_info = RegionInfo(name=region,
							endpoint="ec2.{0}.amazonaws.com".format(region))
	ec2 = EC2Connection(sys.argv[1], sys.argv[2], region=region_info)
	s3 = S3Connection(sys.argv[1], sys.argv[2])
	r53_zone = Route53Zone(sys.argv[1], sys.argv[2], zone_id)

	# the name (and identity) of SOLR
	name = "{0}.{1}".format(userdata['name'],
						os.environ['HOSTED_ZONE_NAME'].rstrip('.'))

	try:
		set_cron(userdata['bucket'])

		# postgres is not running yet, so we have all the freedom we need
		for tablespace in userdata['tablespaces']:
			# keep the size of main for later (WAL)
			if tablespace['name'] == "main":
				size_of_main = tablespace['size']
			create_device(tablespace['device'], tablespace['size'])
			create_mount(tablespace['device'], tablespace['name'])

			add_monitor(tablespace['device'], tablespace['name'])

		# set the correct permissions, and some other necessities
		mount = pg_dir + "main"
		os.system("chmod 0700 {0}".format(mount))

		# prepare the new filesystem for postgres
		os.system("sudo -u postgres /usr/lib/postgresql/9.1/bin/pg_ctl -D {0} initdb".format(mount))
		os.symlink( "/etc/ssl/certs/ssl-cert-snakeoil.pem",
					"{0}/server.crt".format(mount))
		os.symlink( "/etc/ssl/private/ssl-cert-snakeoil.key",
					"{0}/server.key".format(mount))

		# and now, create a separate WAL mount
		# (has to be only now, pg_ctl doesn't like a non-empty postgresql dir)
		os.system("cp -r {0}main/pg_xlog /mnt".format(pg_dir))
		device = "/dev/sdw"
		create_device(device, size_of_main)
		create_mount(device, "main/pg_xlog")
		add_monitor(device, "WAL")
		os.system("cp -r /mnt/pg_xlog/* {0}main/pg_xlog".format(pg_dir))
		os.system("chown -R postgres.postgres {0}main/pg_xlog".format(pg_dir))

		monitor()
	except Exception as e:
		print "{0} could not be prepared ({1}".format(name, e)
