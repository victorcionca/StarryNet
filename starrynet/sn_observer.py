#encoding: utf-8
import os
import datetime
import glob
import numpy as np
from sgp4.api import Satrec, WGS84
from skyfield.api import load, wgs84, EarthSatellite


def _gsl_least_delay(topo_t_shell, gs_cbf, antenna_num):
    gsls_t_shell = [] # [[[ [gsl] for every gs] for every ts] for every shell]
    for shellname, name_lst, sat_cbf_t, isls_t in topo_t_shell:
        gsls_t = []
        for sat_cbf in sat_cbf_t:
            # (gs_num) op (sat_num) -> (gs_num, sat_num)
            dx = np.subtract.outer(gs_cbf[..., 0], sat_cbf[..., 0])
            dy = np.subtract.outer(gs_cbf[..., 1], sat_cbf[..., 1])
            dz = np.subtract.outer(gs_cbf[..., 2], sat_cbf[..., 2])
            dist = np.sqrt(np.square(dx) + np.square(dy) + np.square(dz))
            gsls = []
            for gs_dist in dist:
                gs_dist = gs_dist.flatten()
                #TODO: elevation angle bound
                # bound_mask = gs_dist < bound_dis
                bound_mask = gs_dist == gs_dist
                sat_indices = np.arange(len(gs_dist))[bound_mask]
                gs_dist = gs_dist[bound_mask]
                sorted_sat = gs_dist.argsort()
                gsls.append([
                    # (sat_id, delay in ms)
                    (name_lst[sat_indices[sat]],
                    gs_dist[sat] / (17.31 / 29.5 * 299792.458) * 1000)
                    for sat in sorted_sat[:antenna_num]
                ])
            gsls_t.append(gsls)
        gsls_t_shell.append(gsls_t)
    
    # merge different shell
    # [[gsls for every shell] for every gs] for every t]
    gsls_t = [
        [list() for gid in range(len(gs_cbf))] for t in range(len(gsls_t_shell[0]))
    ]
    for t, gsls in enumerate(gsls_t):
        for gid, gsl_lst in enumerate(gsls):
            for shell_id in range(len(gsls_t_shell)):
                for sat_name, delay in gsls_t_shell[shell_id][t][gid]:
                    if len(gsl_lst) >= antenna_num:
                        break
                    gsl_lst.append((sat_name, delay))
    return gsls_t

def to_cbf(lat_long):# the xyz coordinate system.
    lat_long = np.array(lat_long)
    radius = 6371
    if lat_long.shape[-1] > 2:
        radius += lat_long[..., 2]
    theta_mat = np.radians(lat_long[..., 0])
    phi_mat = np.radians(lat_long[..., 1])
    z_mat = radius * np.sin(theta_mat)
    rho_mat = radius * np.cos(theta_mat)
    x_mat = rho_mat * np.cos(phi_mat)
    y_mat = rho_mat * np.sin(phi_mat)
    return np.stack((x_mat, y_mat, z_mat), -1)

# def _bound_gsl(antenna_elevation, altitude):
#     a = 6371 * np.cos(np.radians(90 + antenna_elevation))
#     return a + np.sqrt(np.square(a) + np.square(altitude) + 2 * altitude * 6371)

def _sat_name(shell_id, orbit_id, sat_id):
    return f'SH{shell_id+1}O{orbit_id+1}S{sat_id+1}'

def _gs_name(gid):
    return f'GS{gid}'

def _isl_grid(sat_cbf_t, shell_id, orbit_num, sat_num):
    # [[ [isl] for every satellite] for every t]
    isls_t = []
    
    sat_cbf_t = sat_cbf_t.reshape(-1, orbit_num, sat_num, 3)
    down_cbf_t = np.roll(sat_cbf_t, -1, 2)
    right_cbf_t = np.roll(sat_cbf_t, -1, 1)
    delay_down_t = np.sqrt(np.sum(np.square(sat_cbf_t - down_cbf_t), -1)) / (
        17.31 / 29.5 * 299792.458) * 1000  # ms
    delay_right_t = np.sqrt(np.sum(np.square(sat_cbf_t - right_cbf_t), -1)) / (
        17.31 / 29.5 * 299792.458) * 1000  # ms
    for delay_down, delay_right in zip(delay_down_t, delay_right_t):
        isl_lst = []
        for oid in range(orbit_num):
            for sid in range(sat_num):
                # down isl
                down_oid = oid
                down_sid = sid + 1 if sid + 1 < sat_num else 0
                # right isl
                right_oid = oid + 1 if oid + 1 < orbit_num else 0
                right_sid = sid
                isl_lst.append([
                    # (sat_name, delay in ms)
                    (_sat_name(shell_id, down_oid, down_sid), delay_down[oid, sid]),
                    # right isl
                    (_sat_name(shell_id, right_oid, right_sid), delay_right[oid, sid]),
                ])
        isls_t.append(isl_lst)
    return isls_t

def _topo_walker_delta(dir, duration, step, shell_lst):
    ts_total = int(duration / step)
    topo_t_shell = []
    ts = load.timescale()
    since = datetime.datetime(1949, 12, 31, 0, 0, 0)
    start = datetime.datetime(2020, 1, 1, 0, 0, 0)
    epoch = (start - since).days
    GM = 3.9860044e14
    R = 6371393
    F = 18
    ts_lst = [i * step for i in range(ts_total)]
    for i, shell in enumerate(shell_lst):
        inclination = shell['inclination'] * 2 * np.pi / 360
        altitude = shell['altitude'] * 1000
        mean_motion = np.sqrt(GM / (R + altitude)**3) * 60
        orbit_number, sat_number = shell['orbit'], shell['sat']
        num_of_sat = orbit_number * sat_number

        sat_lla_t = np.zeros((ts_total, orbit_number * sat_number, 3))
        for oid in range(orbit_number):
            raan = oid / orbit_number * 2 * np.pi
            for sid in range(sat_number):
                mean_anomaly = (sid * 360 / sat_number + oid * 360 * F /
                                num_of_sat) % 360 * 2 * np.pi / 360
                satrec = Satrec()
                satrec.sgp4init(
                    WGS84,  # gravity model
                    'i',  # 'a' = old AFSPC mode, 'i' = improved mode
                    oid * sat_number + sid,  # satnum: Satellite number
                    epoch,  # epoch: days since 1949 December 31 00:00 UT
                    2.8098e-05,  # bstar: drag coefficient (/earth radii)
                    6.969196665e-13,  # ndot: ballistic coefficient (revs/day)
                    0.0,  # nddot: second derivative of mean motion (revs/day^3)
                    0.001,  # ecco: eccentricity
                    0.0,  # argpo: argument of perigee (radians)
                    inclination,  # inclo: inclination (radians)
                    mean_anomaly,  # mo: mean anomaly (radians)
                    mean_motion,  # no_kozai: mean motion (radians/minute)
                    raan,  # nodeo: right ascension of ascending node (radians)
                )
                sat = EarthSatellite.from_satrec(satrec, ts)
                cur = datetime.datetime(2022, 1, 1, 1, 0, 0)
                t_ts = ts.utc(*cur.timetuple()[:5], ts_lst)  # [:4]:minute，[:5]:second
                geocentric = sat.at(t_ts)
                subpoint = wgs84.subpoint(geocentric)
                # list: [subpoint.latitude.degrees] [subpoint.longitude.degrees] [subpoint.elevation.km]
                for t in range(ts_total):
                    sat_lla_t[t, oid * sat_number + sid] = (subpoint.latitude.degrees[t],
                                                            subpoint.longitude.degrees[t],
                                                            subpoint.elevation.km[t])
        
        name_lst = [_sat_name(i, oid, sid)
                    for oid in range(orbit_number) for sid in range(sat_number)]
        pos_dir = os.path.join(dir, shell['name'], 'position')
        os.makedirs(pos_dir, exist_ok=True)
        for t, lla_lst in enumerate(sat_lla_t):
            f = open(os.path.join(pos_dir, '%d.txt' % (t + 1)), 'w')
            for name, lla in zip(name_lst, lla_lst):
                f.write('%s:%f,%f,%f\n' % (name, lla[0], lla[1], lla[2]))
            f.close()
        sat_cbf_t = to_cbf(sat_lla_t)
        isls_t = _isl_grid(sat_cbf_t, i, orbit_number, sat_number)

        topo_t_shell.append((shell['name'], name_lst, sat_cbf_t, isls_t))
    return topo_t_shell

def _topo_arbitrary(dir, duration, step, shell_lst):
    topo_t_shell = []
    for i, shell in enumerate(shell_lst):
        name_lst = []
        sat_cbf_t = []
        isls_t = []
        for t, slot in enumerate(shell['timeslots']):
            sat_lla = []
            sat_names = []
            pos_dir = os.path.join(dir, shell['name'], 'position')
            os.makedirs(pos_dir, exist_ok=True)
            f = open(os.path.join(pos_dir, '%d.txt' % (t + 1)), 'w')
            for sid, node in enumerate(slot['position']):
                print(node)
                lla = (node['latitude'], node['longitude'], node['altitude'])
                sat_lla.append(lla)
                name = f'SH{i+1}SAT{sid+1}'
                f.write(name + (':%f,%f,%f\n' % lla))
                sat_names.append(name)
            f.close()
            sat_cbf = to_cbf(sat_lla)   # np array, sat_num * 3
            sat_cbf_t.append(sat_cbf)

            if len(name_lst) == 0:
                name_lst = sat_names
            elif len(name_lst) != len(sat_names):
                raise RuntimeError("satellites change between slots!")

            isls = [list() for _ in range(len(name_lst))]
            for link in slot['links']:
                sid1, sid2 = link['sat1'], link['sat2']
                if sid1 > sid2:
                    sid1, sid2 = sid2, sid1
                delay = np.sqrt(np.sum(np.square(sat_cbf[sid1] - sat_cbf[sid2])))
                isls[sid1].append((name_lst[sid2], delay))
            isls_t.append(isls)
        topo_t_shell.append((shell['name'], name_lst, sat_cbf_t, isls_t))        
    return topo_t_shell

def _write_link_files(dir, topo_t_shell, gsls_t, GS_lat_long):
    # ISL
    idx_dict = {}
    cnt = 0
    for shell_name, sat_name_lst, sat_cbf_t, isls_t in topo_t_shell:
        isl_dir = os.path.join(dir, shell_name, 'isl')
        os.makedirs(isl_dir, exist_ok=True)
        for file in glob.glob(os.path.join(isl_dir, '*.txt')):
            os.remove(file)
        
        isl_state = [list() for _ in range(len(sat_name_lst))]
        for t, isls in enumerate(isls_t):
            f_state = open(f"{isl_dir}/{t}-state.txt", 'w')
            # for update
            f_update = open(f"{isl_dir}/{t}.txt", 'w')
            for sid, isl_lst in enumerate(isls):
                # one line for each satellite
                f_state.write(f"{sat_name_lst[sid]}:")
                f_state.write(' '.join(f"{isl[0]},{isl[1]:.2f}"
                    for isl in isl_lst))
                f_state.write('\n')

                f_update.write(f"{sat_name_lst[sid]}|")
                old_lst = isl_state[sid]
                old_del = [True] * len(old_lst)
                new_add = [True] * len(isl_lst)
                update = []
                for i, old in enumerate(old_lst):
                    for j, new in enumerate(isl_lst):
                        if old[0] != new[0]:
                            continue
                        # same link, update
                        old_del[i] = False
                        new_add[j] = False
                        if abs(new[1] - old[1]) > 1e-2:
                            update.append(new)
                        else:
                            isl_lst[j] = old
                # del some isls
                f_update.write(' '.join(
                    f"{isl[0]}"
                    for isl, de in zip(old_lst, old_del) if de
                ) + '|')
                # update some isls
                f_update.write(' '.join(
                    f"{isl[0]},{isl[1]:.2f}"
                    for isl in update
                ) + '|')
                # add some isls
                add_lst = []
                for isl, add in zip(isl_lst, new_add):
                    if not add:
                        continue
                    key = f'{sat_name_lst[sid]}-{isl[0]}'
                    if key in idx_dict:
                        idx = idx_dict[key]
                    else:
                        cnt += 1
                        idx_dict[key] = idx = cnt
                    add_lst.append(f"{isl[0]},{isl[1]:.2f},{idx}")
                f_update.write(' '.join(add_lst))
                f_update.write('\n')
                isl_state[sid] = isl_lst
            f_state.close()
            f_update.close()
    # GSL
    gsl_dir = os.path.join(dir, 'GS-' + str(len(GS_lat_long)), 'gsl')
    os.makedirs(gsl_dir, exist_ok=True)
    for file in glob.glob(os.path.join(gsl_dir, '*.txt')):
        os.remove(file)
    
    idx_dict = {}
    cnt = 0
    gsl_state = [list() for _ in range(len(GS_lat_long))]
    for t, gsls in enumerate(gsls_t):
        f_state = open(f"{gsl_dir}/{t}-state.txt", 'w')
        f_update = open(f"{gsl_dir}/{t}.txt", 'w')
        for gid, gsl_lst in enumerate(gsls):
            # one line for each ground station
            f_state.write(f"{_gs_name(gid)}:")
            f_state.write(' '.join(f"{gsl[0]},{gsl[1]:.2f}"
                for gsl in gsl_lst))
            f_state.write('\n')

            f_update.write(f"{_gs_name(gid)}|")
            # del some gsls
            old_lst = gsl_state[gid]
            old_del = [True] * len(old_lst)
            new_add = [True] * len(gsl_lst)
            update = []
            for i, old in enumerate(old_lst):
                for j, new in enumerate(gsl_lst):
                    if old[0] == new[0]:
                        old_del[i] = False
                        new_add[j] = False
                        if abs(new[1] - old[1]) > 1e-2:
                            update.append(new)
                        else:
                            gsl_lst[j] = old # if not update, remain old delay
            f_update.write(' '.join(
                f"{gsl[0]}"
                for gsl, de in zip(old_lst, old_del) if de
            ) + '|')
            # update some gsls
            f_update.write(' '.join(
                f"{gsl[0]},{gsl[1]:.2f}"
                for gsl in update
            ) + '|')
            # add some gsls
            add_lst = []
            for gsl, add in zip(gsl_lst, new_add):
                if not add:
                    continue
                key = f'{_gs_name(gid)}-{gsl[0]}'
                if key in idx_dict:
                    idx = idx_dict[key]
                else:
                    cnt += 1
                    idx_dict[key] = idx = cnt
                add_lst.append(f"{gsl[0]},{gsl[1]:.2f},{idx}")
            f_update.write(' '.join(add_lst))
            f_update.write('\n')
            gsl_state[gid] = gsl_lst
        f_state.close()
        f_update.close()

def gen_topo(dir, duration, step,
             shell_lst, isl_style,
             GS_lat_long, antenna_number, antenna_elevation, gsl_style):
    topo_t_shell = topo_styles[isl_style](dir, duration, step, shell_lst)
    gsls_t = gsl_styles[gsl_style](topo_t_shell, to_cbf(GS_lat_long), antenna_number)
    _write_link_files(dir, topo_t_shell, gsls_t, GS_lat_long)
    sat_names_shell = [shell[1] for shell in topo_t_shell]
    return sat_names_shell

def load_pos(path):
    f = open(path, 'r')
    lla_dict = {}
    for line in f:
        toks = line.strip().split(':')
        lla = tuple(map(float, toks[1].split(',')))
        lla_dict[toks[0]] = lla
    f.close()
    return lla_dict

def load_links_dict(path):
    f = open(path, 'r')
    links_dict = {}
    for line in f:
        toks = line.strip().split(':')
        link_lst = []
        for isl in toks[1].split():
            link_lst.append(isl.split(','))
        links_dict[toks[0]] = link_lst
    f.close()
    return links_dict

#TODO: More ISL styles
topo_styles = {
    'Grid': _topo_walker_delta,
    'Arbitrary': _topo_arbitrary,
}
#TODO: More GSL styles
gsl_styles = {
    'LeastDelay':_gsl_least_delay,
}
