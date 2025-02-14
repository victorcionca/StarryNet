#!/usr/bin/python3
import os
import subprocess
import sys
import re
import json
import glob
import ctypes
# from line_profiler import LineProfiler


"""
Used in the remote machine for link updating, initializing links, damaging and recovering links and other functionalities。
author: Yangtao Deng (dengyt21@mails.tsinghua.edu.cn) and Zeqi Lai (zeqilai@tsinghua.edu.cn) 
"""

ASSIGN_FILENAME = 'assign.json'
PID_FILENAME = 'container_pid.txt'
DAMAGE_FILENAME = 'damage_list.txt'

NOT_ASSIGNED = 'NA'
VXLAN_PORT = 4789
# FIXME
CLONE_NEWNET = 0x40000000
libc = ctypes.CDLL(None)
main_net_fd = os.open('/proc/self/ns/net', os.O_RDONLY)

def _sat_name(shell_id, orbit_id, sat_id):
    return f'SH{shell_id+1}O{orbit_id+1}S{sat_id+1}'

def _gs_name(gid):
    return f'GS{gid+1}'

def _pid_map(pid_path, pop = False):
    global _pid_map_cache
    if _pid_map_cache is None:
        _pid_map_cache = {}
        if not os.path.exists(pid_path):
            print('Error: container index file not found, please create nodes')
            exit(1)
        with open(pid_path, 'r') as f:
            for line in f:
                if len(line) == 0 or line.isspace():
                    continue
                for name_pid in line.strip().split():
                    if name_pid == NOT_ASSIGNED:
                        continue
                    name_pid = name_pid.split(':')
                    _pid_map_cache[name_pid[0]] = name_pid[1]
    if pop:
        ret = _pid_map_cache
        _pid_map_cache = None
        return ret
    return _pid_map_cache

def _get_params(path):
    with open(path, 'r') as f:
        obj = json.load(f)
        sat_mid_dict_shell = obj['sat_mid_shell']
        gs_mid_dict = obj['gs_mid']
        ip_lst = obj['ip']
    return sat_mid_dict_shell, gs_mid_dict, ip_lst

def _parse_isls(path):
    del_lst, update_lst, add_lst = [], [], []
    f = open(path, 'r')
    for line in f:
        toks = line.strip().split('|')
        sat_name = toks[0]
        if len(toks[1]) > 0:
            for isl_sat in toks[1].split(' '):
                del_lst.append((sat_name, isl_sat))
        if len(toks[2]) > 0:
            for isl_sat in toks[2].split(' '):
                sat_delay = isl_sat.split(',')
                update_lst.append((sat_name, sat_delay[0], sat_delay[1]))
        if len(toks[3]) > 0:
            for isl_sat in toks[3].split(' '):
                idx_sat_delay = isl_sat.split(',')
                add_lst.append((sat_name, idx_sat_delay[0], idx_sat_delay[1], int(idx_sat_delay[2])))
    f.close()
    return del_lst, update_lst, add_lst

def _parse_gsls(path):
    del_lst, update_lst, add_lst = [], [], []
    f = open(path, 'r')
    for gid, line in enumerate(f):
        if len(line) == 0 or line.isspace():
            continue
        toks = line.strip().split('|')
        if len(toks[1]) > 0:
            for isl in toks[1].split(' '):
                i_s_o_s_d = isl.split(',')
                idx, shell_id = int(i_s_o_s_d[0]), int(i_s_o_s_d[1])
                oid, sid = int(i_s_o_s_d[2]), int(i_s_o_s_d[3])
                del_lst.append((idx, gid, shell_id, oid, sid, i_s_o_s_d[4]))
        if len(toks[2]) > 0:
            for isl in toks[2].split(' '):
                i_s_o_s_d = isl.split(',')
                idx, shell_id = int(i_s_o_s_d[0]), int(i_s_o_s_d[1])
                oid, sid = int(i_s_o_s_d[2]), int(i_s_o_s_d[3])
                update_lst.append((idx, gid, shell_id, oid, sid, i_s_o_s_d[4]))
        if len(toks[3]) > 0:
            for isl in toks[3].split(' '):
                i_s_o_s_d = isl.split(',')
                idx, shell_id = int(i_s_o_s_d[0]), int(i_s_o_s_d[1])
                oid, sid = int(i_s_o_s_d[2]), int(i_s_o_s_d[3])
                add_lst.append((idx, gid, shell_id, oid, sid, i_s_o_s_d[4]))
    f.close()
    return del_lst, update_lst, add_lst

# name1 in local machine
def _del_link(name1, name2):
    n1_n2 = f"{name2}"
    fd = os.open('/run/netns/' + name1, os.O_RDONLY)
    libc.setns(fd, CLONE_NEWNET)
    os.close(fd)
    subprocess.check_call(('ip', 'link', 'del', n1_n2))

def _init_if(name, if_name, addr, addr6, delay, bw, loss):
    fd = os.open('/run/netns/' + name, os.O_RDONLY)
    libc.setns(fd, CLONE_NEWNET)
    os.close(fd)
    subprocess.check_call(('ip', 'addr', 'add', addr, 'dev', if_name))
    subprocess.check_call(('ip', 'addr', 'add', addr6, 'dev', if_name))
    subprocess.check_call(
        ('tc', 'qdisc', 'add', 'dev', if_name, 'root',
         'netem', 'delay', delay+'ms', 'loss', loss+'%', 'rate', bw+'Gbit')
    )
    subprocess.check_call(('ip', 'link', 'set', if_name, 'up'))

def _update_if(name, if_name, delay, bw, loss):
    fd = os.open('/run/netns/' + name, os.O_RDONLY)
    libc.setns(fd, CLONE_NEWNET)
    os.close(fd)
    update_loss = '100' if name in damage_set else loss
    subprocess.check_call(
        ('tc', 'qdisc', 'change', 'dev', if_name, 'root',
        'netem', 'delay', delay + 'ms', 'rate', bw + 'Gbit', 'loss', update_loss + '%')
    )

def _update_link_intra_machine(name1, name2, delay, bw, loss):
    n1_n2 = f"{name2}"
    n2_n1 = f"{name1}"
    _update_if(name1, n1_n2, delay, bw, loss)
    _update_if(name2, n2_n1, delay, bw, loss)

# name1 in local machine
def _update_link_local(name1, name2, delay, bw, loss):
    n1_n2 = f"{name2}"
    _update_if(name1, n1_n2, delay, bw, loss)

def _add_link_intra_machine(idx, name1, name2, prefix4, prefix6, delay, bw, loss):
    n1_n2 = name2
    n2_n1 = name1
    libc.setns(main_net_fd, CLONE_NEWNET)
    subprocess.check_call(
        ('ip', 'link', 'add', n1_n2, 'netns', name1,
         'type', 'veth', 'peer', n2_n1, 'netns', name2)
    )
    _init_if(name1, n1_n2, prefix4+'.10/24', prefix6 + '::10/48', delay, bw, loss)
    _init_if(name2, n2_n1, prefix4+'.40/24', prefix6 + '::40/48', delay, bw, loss)
    
def _add_link_inter_machine(idx, name1, name2, remote_ip, prefix4, prefix6, delay, bw, loss):
    n1_n2 = name2
    libc.setns(main_net_fd, CLONE_NEWNET)
    subprocess.check_call(
        ('ip', 'link', 'add', n1_n2, 'netns', name1,
         'type', 'vxlan', 'id', str(idx), 'remote', remote_ip, 'dstport', VXLAN_PORT)
    )
    suffix6 = '::10/48' if name1 < name2 else '::40/48'
    _init_if(name1, n1_n2, prefix4+'.10/24', prefix6 + suffix6, delay, bw, loss)

def sn_init_nodes(dir, sat_mid_dict_shell, gs_mid_dict):
    def _load_netns(pid, name):
        netns_link = f'/run/netns/{name}'
        if not os.path.exists(netns_link):
            subprocess.check_call(('ln', '-s', f'/proc/{pid}/ns/net', netns_link))
        sn_container_check_call(
            pid,
            ('sysctl', 'net.ipv6.conf.all.forwarding=1'),
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )
        sn_container_check_call(
            pid, 
            ('sysctl', 'net.ipv4.conf.all.forwarding=1'),
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )

    subprocess.check_call(('sysctl', 'net.ipv4.neigh.default.gc_thresh1=4096'))
    subprocess.check_call(('sysctl', 'net.ipv4.neigh.default.gc_thresh2=8192'))
    subprocess.check_call(('sysctl', 'net.ipv4.neigh.default.gc_thresh3=16384'))
    subprocess.run(('sysctl', 'net.ipv6.neigh.default.gc_thresh1=4096'))
    subprocess.run(('sysctl', 'net.ipv6.neigh.default.gc_thresh2=8192'))
    subprocess.run(('sysctl', 'net.ipv6.neigh.default.gc_thresh3=16384'))

    pid_file = open(dir + '/' + PID_FILENAME, 'w', encoding='utf-8')
    sat_cnt = 0
    for shell_id, mid_dict in enumerate(sat_mid_dict_shell):
        for node, mid in mid_dict.items():  
            if mid != machine_id:
                pid_file.write(NOT_ASSIGNED + ' ')
                continue
            node_dir = f"{dir}/shell{shell_id}/overlay/{node}"
            sat_cnt += 1
            os.makedirs(node_dir, exist_ok=True)
            pid_file.write(node+':'+str(pyctr.container_run(node_dir, node))+' ')
        pid_file.write('\n')
        print(f'[{machine_id}] shell {shell_id}: {sat_cnt} satellites initialized')
    
    gs_lst = []
    overlay_dir = f"{dir}/GS-{len(gs_mid_dict)}/overlay"
    for node, mid in gs_mid_dict.items():
        if mid != machine_id:
            pid_file.write(NOT_ASSIGNED + ' ')
            continue
        gs_lst.append(node)
        node_dir = f'{overlay_dir}/{node}'
        os.makedirs(node_dir, exist_ok=True)
        pid_file.write(node+':'+str(pyctr.container_run(node_dir, node))+' ')
    pid_file.write('\n')
    print(f'[{machine_id}] GS:', ','.join(gs_lst))

    pid_file.close()
    sn_operate_every_node(dir, _load_netns)

def sn_update_network(
        dir, ts, sat_mid_dict_shell, gs_mid_dict, ip_lst,
        isl_bw, isl_loss, gsl_bw, gsl_loss
    ):
    for shell_id, mid_dict in enumerate(sat_mid_dict_shell):
        shell_dir = f"{dir}/shell{shell_id}"
        if not os.path.exists(shell_dir):
            continue
        del_cnt, update_cnt, add_cnt = 0, 0, 0
        del_lst, update_lst, add_lst = _parse_isls(f'{shell_dir}/{ts}.txt')
        for sat_name, isl_sat in del_lst:
            if mid_dict[sat_name] == machine_id:
                del_cnt += 1
                _del_link(sat_name, isl_sat)
            elif mid_dict[isl_sat] == machine_id:
                del_cnt += 1
                _del_link(isl_sat, sat_name)
        for sat_name, isl_sat, delay in update_lst:
            if mid_dict[sat_name] == machine_id:
                update_cnt += 1
                if mid_dict[isl_sat] == machine_id:
                    _update_link_intra_machine(sat_name, isl_sat, delay, isl_bw, isl_loss)
                else:
                    _update_link_local(sat_name, isl_sat, delay, isl_bw, isl_loss)
            elif mid_dict[isl_sat] == machine_id:
                update_cnt += 1
                _update_link_local(isl_sat, sat_name, delay, isl_bw, isl_loss)
        for sat_name, isl_sat, delay, idx in add_lst:
            if mid_dict[sat_name] == machine_id:
                add_cnt += 1
                if mid_dict[isl_sat] == machine_id:
                    _add_link_intra_machine(
                        idx, sat_name, isl_sat,
                        f'10.{idx >> 8}.{idx & 0xFF}', f'2001:{idx >> 8}:{idx & 0xFF}',
                        delay, isl_bw, isl_loss
                    )
                else:
                    _add_link_inter_machine(
                        idx, sat_name, isl_sat, ip_lst[mid_dict[isl_sat]],
                        f'10.{idx >> 8}.{idx & 0xFF}', f'2001:{idx >> 8}:{idx & 0xFF}',
                        delay, isl_bw, isl_loss
                    )
            elif mid_dict[isl_sat] == machine_id:
                add_cnt += 1
                _add_link_inter_machine(
                    idx, isl_sat, sat_name, ip_lst[mid_dict[sat_name]],
                    f'10.{idx >> 8}.{idx & 0xFF}', f'2001:{idx >> 8}:{idx & 0xFF}',
                    delay, isl_bw, isl_loss
                )
        print(f"[{machine_id}] Shell {shell_id}:",
              f"{del_cnt} deleted, {update_cnt} updated, {add_cnt} added.")

    gs_dir = f"{dir}/GS-{len(gs_mid_dict)}"
    return
    if not os.path.exists(gs_dir):
        return
    del_cnt, update_cnt, add_cnt = 0, 0, 0
    del_lst, update_lst, add_lst = _parse_gsls(f'{gs_dir}/{ts}.txt')
    for idx, gid, shell_id, oid, sid, delay in del_lst:
        orbit_num, shell_name, sat_mid = sat_mid_lst[shell_id]
        if gs_mid[gid] == machine_id:
            del_cnt += 1
            _del_link(idx, _gs_name(gid), _sat_name(shell_id, oid, sid))
        elif sat_mid[sid] == machine_id:
            del_cnt += 1
            _del_link(idx, _sat_name(shell_id, isl_oid, isl_sid), _gs_name(gid))
    for idx, gid, shell_id, oid, sid, delay in update_lst:
        orbit_num, shell_name, sat_mid = sat_mid_lst[shell_id]
        if gs_mid[gid] == machine_id:
            update_cnt += 1
            if sat_mid[sid] == machine_id:
                _update_link_intra_machine(
                    idx,
                    _gs_name(gid), _sat_name(shell_id, oid, sid),
                    delay, gsl_bw, gsl_loss
                )
            else:
                _update_link_local(
                    idx,
                    _gs_name(gid), _sat_name(shell_id, oid, sid),
                    delay, gsl_bw, gsl_loss
                )
        elif sat_mid[sid] == machine_id:
            update_cnt += 1
            _update_link_local(
                idx,
                _sat_name(shell_id, oid, sid), _gs_name(gid),
                delay, gsl_bw, gsl_loss
            )
    for idx, gid, shell_id, oid, sid, delay in add_lst:
        orbit_num, shell_name, sat_mid = sat_mid_lst[shell_id]
        if gs_mid[gid] == machine_id:
            add_cnt += 1
            if sat_mid[sid] == machine_id:
                _add_link_intra_machine(
                    idx,
                    _gs_name(gid), _sat_name(shell_id, oid, sid),
                    f'9.{idx >> 8}.{idx & 0xFF}', delay, gsl_bw, gsl_loss
                )
            else:
                _add_link_inter_machine(
                    idx,
                    _gs_name(gid), _sat_name(shell_id, oid, sid), ip_lst[sat_mid[sid]],
                    f'9.{idx >> 8}.{idx & 0xFF}', delay, gsl_bw, gsl_loss
                )
        elif sat_mid[sid] == machine_id:
            add_cnt += 1
            _add_link_inter_machine(
                idx,
                _sat_name(shell_id, oid, sid), _gs_name(gid), ip_lst[gs_mid[gid]],
                f'9.{idx >> 8}.{idx & 0xFF}', delay, gsl_bw, gsl_loss
            )
    print(f"[{machine_id}] GSL:",
          f"{del_cnt} deleted, {update_cnt} updated, {add_cnt} added.")

def sn_container_check_call(pid, cmd, *args, **kwargs):
    subprocess.check_call(
        ('nsenter', '-m', '-u', '-i', '-n', '-p', '-t', pid, *cmd),
        *args, **kwargs
    )

def sn_container_run(pid, cmd, *args, **kwargs):
    subprocess.run(
        ('nsenter', '-m', '-u', '-i', '-n', '-p', '-t', pid, *cmd),
        *args, **kwargs
    )

def sn_container_check_output(pid, cmd, *args, **kwargs):
    return subprocess.check_output(
        ('nsenter', '-m', '-u', '-i', '-n', '-p', '-t', pid, *cmd),
        *args, **kwargs
    )

def sn_operate_every_node(dir, func, *args):
    pid_map = _pid_map(dir + '/' + PID_FILENAME)
    for name, pid in pid_map.items():
        func(pid, name, *args)

def get_IP(dir, node):
    pid = _pid_map(f"{dir}/{PID_FILENAME}")[node]
    addr_lst = subprocess.check_output(
        ('nsenter', '-m', '-u', '-i', '-n', '-p', '-t', pid,
        'ip', '-br', 'addr', 'show')
    ).decode().splitlines()
    for dev_state_addrs in addr_lst:
        dev_state_addrs = dev_state_addrs.split()
        if len(dev_state_addrs) < 3:
            continue
        print(dev_state_addrs[0].split('@')[0], dev_state_addrs[2])

def sn_init_route_daemons(dir, conf_path, nodes):
    def _init_route_daemon(pid, name):
        bird_ctl_path = conf_path[:conf_path.rfind('/')] + '/bird.ctl'
        sn_container_run(pid, ('bird', '-c', conf_path, '-s', bird_ctl_path))
    if nodes == 'all':
        sn_operate_every_node(dir, _init_route_daemon)
    else:
        pid_map = _pid_map(f"{dir}/{PID_FILENAME}")
        nodes_lst = nodes.split(',')
        for node in nodes_lst:
            _init_route_daemon(pid_map[node], node)

def sn_ping(dir, src, dst):
    pid_map = _pid_map(f"{dir}/{PID_FILENAME}")
    # suppose src in this machine
    src_pid = pid_map[src]
    # TODO: dst in other machine
    dst_pid = pid_map[dst]
    
    dst_addr_lst = subprocess.check_output(
        ('nsenter', '-m', '-u', '-i', '-n', '-p', '-t', dst_pid,
        'ip', '-br', 'addr', 'show')
    ).decode().splitlines()
    for dev_state_addrs in dst_addr_lst:
        dev_state_addrs = dev_state_addrs.split()
        if dev_state_addrs[0] == 'lo':
            continue
        dst_addr = dev_state_addrs[2]
        if dev_state_addrs[0].split('@')[0] == src:
            break
    dst_addr = dst_addr[:dst_addr.rfind('/')]
    print('ping', src, dst_addr)

    subprocess.run(
        ('nsenter', '-m', '-u', '-i', '-n', '-p', '-t', src_pid,
         'ping', '-c', '4', '-i', '0.01', dst_addr),
         stdout=sys.stdout, stderr=subprocess.STDOUT
    )

def sn_iperf(dir, src, dst):
    pid_map = _pid_map(f"{dir}/{PID_FILENAME}")
    # suppose src in this machine
    src_pid = pid_map[src]
    # TODO: dst in other machine
    dst_pid = pid_map[dst]
    
    dst_addr_lst = subprocess.check_output(
        ('nsenter', '-m', '-u', '-i', '-n', '-p', '-t', dst_pid,
        'ip', '-br', 'addr', 'show')
    ).decode().splitlines()
    for dev_state_addrs in dst_addr_lst:
        dev_state_addrs = dev_state_addrs.split()
        if dev_state_addrs[0] == 'lo':
            continue
        dst_addr = dev_state_addrs[2]
        if dev_state_addrs[0].split('@')[0] == src:
            break
    dst_addr = dst_addr[:dst_addr.rfind('/')]

    server = subprocess.Popen(
        ('nsenter', '-m', '-u', '-i', '-n', '-p', '-t', dst_pid,
         'iperf3', '-s'),
         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
    )
    subprocess.run(
        ('nsenter', '-m', '-u', '-i', '-n', '-p', '-t', src_pid,
         'iperf3', '-c', dst_addr, '-t5'),
         stdout=sys.stdout, stderr=subprocess.STDOUT
    )
    server.terminate()

def sn_sr(dir, src, dst, nxt):
    pid_map = _pid_map(f"{dir}/{PID_FILENAME}")
    # suppose src in this machine
    src_pid = pid_map[src]
    # TODO: dst in other machine
    dst_pid = pid_map[dst]

    dst_addr_lst = subprocess.check_output(
        ('nsenter', '-m', '-u', '-i', '-n', '-p', '-t', dst_pid,
        'ip', '-br', 'addr', 'show')
    ).decode().splitlines()
    for dev_state_addrs in dst_addr_lst:
        dev_state_addrs = dev_state_addrs.split()
        if dev_state_addrs[0] == 'lo':
            continue
        dst_addr = dev_state_addrs[2]
        dst_prefix = dst_addr[:dst_addr.rfind('.')] + '.0/24'
        subprocess.run(
            ('nsenter', '-n', '-t', src_pid,
            'ip', 'route', 'add', dst_prefix, 'dev', nxt),
            stdout=sys.stdout, stderr=subprocess.STDOUT
        )

def sn_check_route(dir, node):
    pid_map = _pid_map(f"{dir}/{PID_FILENAME}")
    subprocess.run(
        ('nsenter', '-n', '-t', pid_map[node],
        'route'),
        stdout=sys.stdout, stderr=subprocess.STDOUT
    )

def sn_clean(dir):
    damage_file = f"{dir}/{DAMAGE_FILENAME}"
    if os.path.exists(damage_file):
        os.remove(damage_file)
    for ns_link in glob.glob(f"/run/netns/SH*O*S*"):
        if os.path.islink(ns_link):
            os.remove(ns_link)
    for ns_link in glob.glob(f"/run/netns/G*"):
        if os.path.islink(ns_link):
            os.remove(ns_link)
    pid_file = f"{dir}/{PID_FILENAME}"
    if not os.path.exists(pid_file):
        return
    pid_map = _pid_map(pid_file, True)
    for pid in pid_map.values():
        if pid == NOT_ASSIGNED:
            continue
        try:
            os.kill(int(pid), 9)
        except ProcessLookupError:
            pass
    os.remove(pid_file)

def _change_sat_link_loss(pid, loss):
    out = subprocess.check_output(
        ('nsenter', '-t', pid, '-n',
        'tc', 'qdisc', 'show')).decode()
    for line in out.splitlines():
        line = line.strip()
        if len(line) == 0 or line.startswith('lo'):
            continue
        qdisc_netem_hd_dev_name_ = line.split()
        dev_name = qdisc_netem_hd_dev_name_[4]
        delay = qdisc_netem_hd_dev_name_[qdisc_netem_hd_dev_name_.index('delay') + 1]
        subprocess.check_call(
            ('nsenter', '-t', pid, '-n',
            'tc', 'qdisc', 'change', 'dev', dev_name, 'root',
            'netem', 'delay', delay, 'loss', loss+'%'))

def sn_damage(dir, random_list):
    with open(f"{dir}/{DAMAGE_FILENAME}", 'a') as f:
        for node in random_list:
            pid_mat = _pid_map(f"{dir}/{PID_FILENAME}")
            pid = pid_mat[node]
            _change_sat_link_loss(pid, '100')
            f.write(node + '\n')
            print(f'[{machine_id}] damage node: {node}')

def sn_recover(dir, sat_loss):
    damage_file = f"{dir}/{DAMAGE_FILENAME}"
    if not os.path.exists(damage_file):
        return
    with open(f"{dir}/{DAMAGE_FILENAME}", 'r') as f:
        for node in f:
            pid_mat = _pid_map(f"{dir}/{PID_FILENAME}")
            pid = pid_mat[node.strip()]
            _change_sat_link_loss(pid, sat_loss)
            print(f'[{machine_id}] recover sat: {node}')
    os.remove(damage_file)

if __name__ == '__main__':
    _pid_map_cache = None

    if len(sys.argv) < 2:
        print('Usage: sn_orchestrater.py <command> ...')
        exit(1)
    cmd = sys.argv[1]
    if cmd == 'exec':
        pid_map = _pid_map(os.path.dirname(__file__) + '/' + PID_FILENAME)
        if len(sys.argv) < 4:
            print('Usage: sn_orchestrater.py exec <node> <command> ...')
            exit(1)
        if sys.argv[2] not in pid_map:
            print('Error:', sys.argv[3], 'not found')
            exit(1)
        exit(subprocess.run(
            ('nsenter', '-a', '-t', pid_map[sys.argv[2]],
            *sys.argv[3:])
        ).returncode)

    if len(sys.argv) < 3:
        machine_id = None
    else:
        try:
            machine_id = int(sys.argv[2])
        except:
            machine_id = None
    if len(sys.argv) < 4:
        workdir = os.path.dirname(__file__)
    else:
        workdir = sys.argv[3]

    # C module
    try:
        import pyctr
    except ModuleNotFoundError:
        subprocess.check_call(
            "cd " + workdir + " && "
            "gcc $(python3-config --cflags --ldflags)"
            "-shared -fPIC -O2 pyctr.c -o pyctr.so",
            shell=True
        )
        import pyctr
    
    damage_set = set()
    damage_file = workdir + '/' + DAMAGE_FILENAME
    if os.path.exists(damage_file):
        with open(workdir + '/' + DAMAGE_FILENAME, 'r') as f:
            for line in f:
                damage_set.add(line.strip())

    sat_mid_dict_shell, gs_mid_dict, ip_lst = _get_params(workdir + '/' + ASSIGN_FILENAME)
    if cmd == 'nodes':
        sn_clean(workdir)
        sn_init_nodes(workdir, sat_mid_dict_shell, gs_mid_dict)
    elif cmd == 'list':
        print(f"{'NODE':<20} STATE")
        for name in _pid_map(workdir + '/' + PID_FILENAME):
            print(f"{name:<20} {'Damaged' if name in damage_set else 'OK'}")
    elif cmd == 'networks':
        # lp = LineProfiler()
        # sn_update_network = lp(sn_update_network)
        # lp.add_function(_update_link_intra_machine)
        sn_update_network(
            workdir, sys.argv[4], sat_mid_dict_shell, gs_mid_dict, ip_lst,
            sys.argv[5], sys.argv[6], sys.argv[7], sys.argv[8]
        )
        # with open('report.txt', 'w') as f:
            # lp.print_stats(f)
    elif cmd == 'routed':
        sn_init_route_daemons(workdir, workdir + '/bird.conf', sys.argv[4])
    elif cmd == 'IP':
        get_IP(workdir, sys.argv[4])
    elif cmd == 'damage':
        sn_damage(workdir, sys.argv[4].split(','))
    elif cmd == 'recovery':
        sn_recover(workdir, sys.argv[4])
    elif cmd == 'clean':
        sn_clean(workdir)
    elif cmd == 'ping':
        sn_ping(workdir, sys.argv[4], sys.argv[5])
    elif cmd == 'iperf':
        sn_iperf(workdir, sys.argv[4], sys.argv[5])
    elif cmd == 'sr':
        sn_sr(workdir, sys.argv[4], sys.argv[5], sys.argv[6])
    elif cmd == 'rtable':
        sn_check_route(workdir, sys.argv[4])
    else:
        print('Unknown command')
    os.close(main_net_fd)
