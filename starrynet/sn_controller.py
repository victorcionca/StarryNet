import time
import threading
import math
import json
import requests

from starrynet.sn_utils import *

ASSIGN_FILENAME = 'assign.json'
LINK_FILENAME = 'link.json'

class Remote:
    def __init__(self, id, host, port, username, password):
        self.id = id

        self.ssh, self.sftp = sn_connect_remote(
            host = host,
            port = port,
            username = username,
            password = password,
        )

        self.dir = sn_remote_cmd(self.ssh, 'echo ~/SN')
        sn_remote_cmd(self.ssh, 'mkdir ' + self.dir)

        self.sftp.put(
            os.path.join(os.path.dirname(__file__), 'sn_remote.py'),
            self.dir + '/sn_remote.py'
        )
        self.sftp.put(
            os.path.join(os.path.dirname(__file__), 'pyctr.c'),
            self.dir + '/pyctr.c'
        )
        self.sftp.put(
            ASSIGN_FILENAME,
            self.dir + '/' + ASSIGN_FILENAME
        )
        sn_remote_wait_output(
            self.ssh,
            f"python3 {self.dir}/sn_remote.py nodes {self.id} {self.dir}"
        )

    def update_network(self, del_links, add_links, update_links):
        self.sftp.put(LINK_FILENAME, self.dir + '/' + LINK_FILENAME)
        sn_remote_wait_output(
            self.ssh,
            f"python3 {self.dir}/sn_remote.py networks {self.id} {self.dir} "
        )

class TopoSync():

    def __init__(self, config_path):
        with open(config_path, 'r') as f:
            config = json.load(f)
        self.constellation = config['constellation']
        self.api_url = config['api_url']
        self.time_step = config['step']
        self.machine_lst = config['machines']
        self.node_init = False
        self.link_dict = {}
    
    def run(self):
        self.last_links = set()
        last_t = time.time()
        while True:
            t = time.time()
            if t - last_t < self.time_step:
                time.sleep(last_t + self.time_step - t)
                t = time.time()
            
            print('Time: ', t, '\n')
            last_t = t
            res = requests.get(self.api_url)
                    #    + f'?time={t}&constellation={self.constellation}&timestep={self.time_step}')
            node_info = json.loads(res.text)
            new_links, del_links, add_links, update_links = self._parse(node_info)

            with open(LINK_FILENAME, 'w') as f:
                json.dump(
                    {'del_links': del_links, 'add_links': add_links, 'update_links': update_links},
                    f
                )

            rmt_threads = []
            for rmt in self.remote_lst:
                thread = threading.Thread(
                    target=rmt.update_network,
                    args=(del_links, add_links, update_links)
                )
                thread.start()
                rmt_threads.append(thread)

            self.last_links = new_links

            for thread in rmt_threads:
                thread.join()

    def _parse(self, node_info):
        EPS = 0.01
        def distance(lla1, lla2):
            RADIUS = 6371

            lat_rad1, lng_rad1 = lla1[0] * math.pi / 180, lla1[1] * math.pi / 180
            lat_rad2, lng_rad2 = lla2[0] * math.pi / 180, lla2[1] * math.pi / 180

            sa = math.sin((lat_rad1 - lat_rad2)/2)
            sb = math.sin((lng_rad1 - lng_rad2)/2)
            # FIXME: Altitude
            return 2 * RADIUS * math.asin(math.sqrt(
                sa * sa + math.cos(lat_rad1) * math.cos(lat_rad2) * sb * sb
            ))

        if not self.node_init:
            self._init_node(node_info)

        new_links = set()
        for isl in node_info['link_ISL']:
            src_id, dst_id = isl['src'], isl['dst']
            if src_id == dst_id:
                continue
            if src_id > dst_id:
                src_id, dst_id = dst_id, src_id
            new_links.add((src_id, dst_id))
        
        for gsl in node_info['link_GSL_Up']:
            # GS - SAT
            src_id, dst_id = gsl['src'] + self.sat_nr, gsl['dst']
            new_links.add((src_id, dst_id))

        # assign GS to machine of the first connected SAT
        if not self.node_init:
            print('Initializing ...')
            for gsl in node_info['link_GSL_Up']:
                src_id, dst_id = gsl['src'] + self.sat_nr, gsl['dst']
                if self.node_mid[src_id] is not None:
                    continue
                self.node_mid[src_id] = self.node_mid[dst_id]
            
            with open(ASSIGN_FILENAME, 'w') as f:
                json.dump(
                    {
                        'node_name':self.node_name,
                        'node_mid': self.node_mid,
                        'ip': [machine['IP'] for machine in self.machine_lst],
                    },
                    f
                )

            self.remote_lst = []            
            for mid, machine in enumerate(self.machine_lst):
                self.remote_lst.append(Remote(
                    mid,
                    machine['IP'],
                    machine['port'],
                    machine['username'],
                    machine['password'],
                ))
            self.node_init = True
        
        del_set = self.last_links.difference(new_links)
        add_set = new_links.difference(self.last_links)
        remain_set = new_links.intersection(self.last_links)

        del_links = [de for de in del_set]
        add_links = []
        update_links = []
        for add in add_set:
            delay_ms = (distance(self.node_lla[add[0]], self.node_lla[add[1]]) 
                        / 299.792458)
            if add in self.link_dict:
                idx = self.link_dict[add][0]
            else:
                idx = len(self.link_dict) + 1
                self.link_dict[add] = [idx, delay_ms]
            add_links.append((add[0], add[1], delay_ms, idx))

        for remain in remain_set:
            delay_ms = (distance(self.node_lla[remain[0]], self.node_lla[remain[1]]) 
                        / 299.792458)
            cur_delay = self.link_dict[remain][1]
            if abs(delay_ms - cur_delay) <= EPS:
                continue
            update_links.append((remain[0], remain[1], delay_ms))

        return new_links, del_links, add_links, update_links

    def _init_node(self, node_info):
        self.node_name = []
        self.node_lla = []
        self.node_mid = [None] * (len(node_info['sat']) + len(node_info['ground']))

        sat_per_machine = (len(node_info['sat']) + len(self.machine_lst) - 1) // len(self.machine_lst)
        
        for idx, sat in enumerate(node_info['sat']):
            if idx != sat['id']:
                raise RuntimeError("'id' of sat is not incremented")
            self.node_name.append(f'SAT{idx}')
            self.node_lla.append((float(sat['lat']), float(sat['lon']), float(sat['alt'])))
        
        for i in range(len(self.machine_lst)):
            for j in range(i * sat_per_machine, min((i+1) * sat_per_machine, len(self.node_name))):
                self.node_mid[j] = i            
        
        self.sat_nr = len(self.node_name)

        for idx, gs in enumerate(node_info['ground']):
            if idx != gs['id']:
                raise RuntimeError("'id' of gs is not incremented")
            self.node_name.append(f'GS{idx}')
            self.node_lla.append((float(gs['lat']), float(gs['lon']), float(gs['alt'])))
