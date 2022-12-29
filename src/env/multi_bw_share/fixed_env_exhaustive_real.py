import itertools

from scipy.optimize import minimize, LinearConstraint
from statsmodels.tsa.api import ExponentialSmoothing
import pandas as pd
import numpy as np
import copy

from env.multi_bw_share.satellite import Satellite
from env.multi_bw_share.user import User
from util.constants import EPSILON

VIDEO_BIT_RATE = [300, 750, 1200, 1850, 2850, 4300]
M_IN_K = 1000.0

MILLISECONDS_IN_SECOND = 1000.0
B_IN_MB = 1000000.0
BITS_IN_BYTE = 8.0
RANDOM_SEED = 42
VIDEO_CHUNCK_LEN = 4000.0  # millisec, every time add this amount to buffer
BITRATE_LEVELS = 6
TOTAL_VIDEO_CHUNCK = 48
BUFFER_THRESH = 60.0 * MILLISECONDS_IN_SECOND  # millisec, max buffer limit
DRAIN_BUFFER_SLEEP_TIME = 500.0  # millisec
PACKET_PAYLOAD_PORTION = 0.95
LINK_RTT = 80  # millisec
PACKET_SIZE = 1500  # bytes
VIDEO_SIZE_FILE = '../../data/video_data/envivio/video_size_'
DEFAULT_QUALITY = 2  # default video quality without agent
BITRATE_WEIGHT = 2

CHUNK_TIL_VIDEO_END_CAP = 48.0

MPC_FUTURE_CHUNK_COUNT = 2
MPC_PAST_CHUNK_COUNT = 5

# LEO SETTINGS
HANDOVER_DELAY = 0.2  # sec
HANDOVER_WEIGHT = 1
SCALE_VIDEO_SIZE_FOR_TEST = 20
SCALE_VIDEO_LEN_FOR_TEST = 2

# Multi-agent setting
NUM_AGENTS = None

QUALITY_FACTOR = 1
REBUF_PENALTY = 4.3  # pensieve: 4.3  # 1 sec rebuffering -> 3 Mbps
SMOOTH_PENALTY = 1

SAT_STRATEGY = "resource-fair"
SAT_STRATEGY = "ratio-based"

# SNR_MIN = 90


class Environment:
    def __init__(self, all_cooked_time, all_cooked_bw, random_seed=RANDOM_SEED, num_agents=NUM_AGENTS):
        assert len(all_cooked_time) == len(all_cooked_bw)

        np.random.seed(random_seed)
        self.num_agents = num_agents

        self.all_cooked_time = all_cooked_time
        self.all_cooked_bw = all_cooked_bw

        # pick a random trace file
        self.trace_idx = 0
        self.cooked_time = self.all_cooked_time[self.trace_idx]
        # self.cooked_bw = self.all_cooked_bw[self.trace_idx]

        self.cooked_bw = self.all_cooked_bw[self.trace_idx]

        # self.last_quality = DEFAULT_QUALITY
        self.last_quality = [DEFAULT_QUALITY for _ in range(self.num_agents)]

        self.mahimahi_start_ptr = 1
        # randomize the start point of the trace
        # note: trace file starts with time 0
        self.mahimahi_ptr = [1 for _ in range(self.num_agents)]
        self.last_mahimahi_time = [self.cooked_time[self.mahimahi_start_ptr - 1] for _ in range(self.num_agents)]

        # Centralization
        self.user_qoe_log = [{} for _ in range(self.num_agents)]
        self.num_of_user_sat = {}
        self.num_sat_info = {}
        self.cur_satellite = {}

        sat_list = []
        for user_id in range(self.num_agents):
            for sat_id, sat_bw in self.cooked_bw[user_id].items():
                if sat_id not in sat_list:
                    self.num_sat_info[sat_id] = [0 for _ in range(len(sat_bw))]
                    self.num_of_user_sat[sat_id] = 0
                    self.cur_satellite[sat_id] = Satellite(sat_id, sat_bw, user_id, SAT_STRATEGY)
                    sat_list.append(sat_id)
                else:
                    self.cur_satellite[sat_id].add_bw(sat_bw, user_id)

        self.cur_user = []
        for agent_id in range(self.num_agents):
            # self.cur_user.append(User(agent_id, SNR_MIN))
            self.cur_user.append(User(agent_id, None))

        # print(self.num_sat_info)

        self.stored_num_of_user_sat = None
        self.stored_mahimahi_ptr = None
        self.stored_last_mahimahi_time = None
        self.stored_buffer_size = None
        self.stored_video_chunk_counter = None
        self.stored_cur_sat_id = None
        self.stored_cur_satellite = None

        # exit(1)
        # multiuser setting
        self.cur_sat_id = []
        self.prev_sat_id = [None for _ in range(self.num_agents)]
        for agent in range(self.num_agents):
            cur_sat_id = self.get_best_sat_id(agent)
            self.cur_sat_id.append(cur_sat_id)
            self.update_sat_info(cur_sat_id, self.mahimahi_ptr[agent], agent, 1)

        self.video_chunk_counter = [0 for _ in range(self.num_agents)]
        self.buffer_size = [0 for _ in range(self.num_agents)]
        self.video_chunk_counter_sent = [0 for _ in range(self.num_agents)]
        self.video_chunk_remain = [TOTAL_VIDEO_CHUNCK for _ in range(self.num_agents)]
        self.end_of_video = [False for _ in range(self.num_agents)]
        self.next_video_chunk_sizes = [[] for _ in range(self.num_agents)]
        # self.next_sat_bandwidth = [[] for _ in range(self.num_agents)]
        self.next_sat_id = [[] for _ in range(self.num_agents)]
        self.delay = [0 for _ in range(self.num_agents)]

        self.bit_rate = None
        self.download_bw = [[] for _ in range(self.num_agents)]
        self.past_download_ests = [[] for _ in range(self.num_agents)]
        self.past_download_bw_errors = [[] for _ in range(self.num_agents)]
        self.past_bw_ests = [{} for _ in range(self.num_agents)]
        self.past_bw_errors = [{} for _ in range(self.num_agents)]

        self.video_size = {}  # in bytes
        for bitrate in range(BITRATE_LEVELS):
            self.video_size[bitrate] = []
            with open(VIDEO_SIZE_FILE + str(bitrate)) as f:
                for line in f:
                    self.video_size[bitrate].append(int(line.split()[0]))

    def get_video_chunk(self, quality, agent, model_type, runner_up_sat_id, ho_stamp, do_mpc=False):

        assert quality >= 0
        assert quality < BITRATE_LEVELS

        # update noise of agent SNR
        # elf.cur_user[agent].update_snr_noise()
        cooked_bw = self.cooked_bw[agent]

        video_chunk_size = self.video_size[quality][self.video_chunk_counter[agent]]

        # use the delivery opportunity in mahimahi
        # delay = 0.0  # in ms
        delay = self.delay[agent]  # in ms
        self.delay[agent] = 0
        video_chunk_counter_sent = 0  # in bytes
        end_of_network = False
        is_handover = False

        if ho_stamp == 0:
            is_handover = True
            delay += HANDOVER_DELAY
            # self.connection[self.cur_sat_id[agent]] = -1
            # self.connection[new_sat_id] = agent
            # update sat info
            runner_up_sat_id, _ = self.get_runner_up_sat_id(agent, method="harmonic-mean")
            self.update_sat_info(self.cur_sat_id[agent], self.mahimahi_ptr[agent], agent, -1)
            self.update_sat_info(runner_up_sat_id, self.mahimahi_ptr[agent], agent, 1)
            self.prev_sat_id[agent] = self.cur_sat_id[agent]
            self.cur_sat_id[agent] = runner_up_sat_id

            self.download_bw[agent] = []

        self.last_quality[agent] = quality

        while True:  # download video chunk over mahimahi
            throughput = self.cur_satellite[self.cur_sat_id[agent]].data_rate(self.cur_user[agent],
                                                                              self.mahimahi_ptr[agent])

            if throughput == 0.0:
                # Do the forced handover
                # Connect the satellite that has the best serving time
                sat_id = self.get_best_sat_id(agent, self.mahimahi_ptr[agent])
                self.update_sat_info(sat_id, self.mahimahi_ptr[agent], agent, 1)
                self.update_sat_info(self.cur_sat_id[agent], self.mahimahi_ptr[agent], agent, -1)

                self.switch_sat(agent, sat_id)
                delay += HANDOVER_DELAY
                is_handover = True
                self.download_bw[agent] = []
                print("Forced Handover")
                throughput = self.cur_satellite[self.cur_sat_id[agent]].data_rate(self.cur_user[agent],
                                                                                  self.mahimahi_ptr[agent])

            duration = self.cooked_time[self.mahimahi_ptr[agent]] \
                       - self.last_mahimahi_time[agent]

            packet_payload = throughput * duration * PACKET_PAYLOAD_PORTION

            if video_chunk_counter_sent + packet_payload > video_chunk_size:
                fractional_time = (video_chunk_size - video_chunk_counter_sent) / \
                                  throughput / PACKET_PAYLOAD_PORTION
                delay += fractional_time
                self.last_mahimahi_time[agent] += fractional_time
                break

            video_chunk_counter_sent += packet_payload
            delay += duration
            self.last_mahimahi_time[agent] = self.cooked_time[self.mahimahi_ptr[agent]]

            self.mahimahi_ptr[agent] += 1

            if self.mahimahi_ptr[agent] >= len(cooked_bw[self.cur_sat_id[agent]]):
                # loop back in the beginning
                # note: trace file starts with time 0
                self.mahimahi_ptr[agent] = 1
                self.last_mahimahi_time[agent] = 0
                self.end_of_video[agent] = True
                end_of_network = True
                break

        delay *= MILLISECONDS_IN_SECOND
        delay += LINK_RTT

        # rebuffer time
        rebuf = np.maximum(delay - self.buffer_size[agent], 0.0)

        # update the buffer
        self.buffer_size[agent] = np.maximum(self.buffer_size[agent] - delay, 0.0)

        # add in the new chunk
        self.buffer_size[agent] += VIDEO_CHUNCK_LEN

        # sleep if buffer gets too large
        sleep_time = 0
        if self.buffer_size[agent] > BUFFER_THRESH:
            # exceed the buffer limit
            # we need to skip some network bandwidth here
            # but do not add up the delay
            drain_buffer_time = self.buffer_size[agent] - BUFFER_THRESH
            sleep_time = np.ceil(drain_buffer_time / DRAIN_BUFFER_SLEEP_TIME) * \
                         DRAIN_BUFFER_SLEEP_TIME
            self.buffer_size[agent] -= sleep_time

            while True:
                if self.mahimahi_ptr[agent] >= len(cooked_bw[self.cur_sat_id[agent]]):
                    # loop back in the beginning
                    # note: trace file starts with time 0
                    self.mahimahi_ptr[agent] = 1
                    self.last_mahimahi_time[agent] = 0
                    self.end_of_video[agent] = True
                    end_of_network = True
                    break

                duration = self.cooked_time[self.mahimahi_ptr[agent]] \
                           - self.last_mahimahi_time[agent]
                if duration > sleep_time / MILLISECONDS_IN_SECOND:
                    self.last_mahimahi_time[agent] += sleep_time / MILLISECONDS_IN_SECOND
                    break
                sleep_time -= duration * MILLISECONDS_IN_SECOND
                self.last_mahimahi_time[agent] = self.cooked_time[self.mahimahi_ptr[agent]]
                self.mahimahi_ptr[agent] += 1

                throughput = self.cur_satellite[self.cur_sat_id[agent]].data_rate(self.cur_user[agent],
                                                                                  self.mahimahi_ptr[agent])
                if throughput == 0.0:
                    # Do the forced handover
                    # Connect the satellite that has the best serving time
                    sat_id = self.get_best_sat_id(agent, self.mahimahi_ptr[agent])
                    self.update_sat_info(sat_id, self.mahimahi_ptr[agent], agent, 1)
                    self.update_sat_info(self.cur_sat_id[agent], self.mahimahi_ptr[agent], agent, -1)
                    self.switch_sat(agent, sat_id)
                    is_handover = True
                    print("Forced Handover")
                    throughput = self.cur_satellite[self.cur_sat_id[agent]].data_rate(self.cur_user[agent],
                                                                                      self.mahimahi_ptr[agent])

        # the "last buffer size" return to the controller
        # Note: in old version of dash the lowest buffer is 0.
        # In the new version the buffer always have at least
        # one chunk of video
        return_buffer_size = self.buffer_size[agent]

        self.video_chunk_counter[agent] += 1
        video_chunk_remain = TOTAL_VIDEO_CHUNCK - self.video_chunk_counter[agent]

        cur_sat_bw_logs, next_sat_bandwidth, next_sat_id, next_sat_bw_logs, connected_time = self.get_next_sat_info(
            agent, self.mahimahi_ptr[agent])
        if self.video_chunk_counter[agent] >= TOTAL_VIDEO_CHUNCK or end_of_network:
            self.end_of_video[agent] = True
            self.buffer_size[agent] = 0
            self.video_chunk_counter[agent] = 0

            # Refresh satellite info
            # self.connection[self.cur_sat_id[agent]] = -1
            # self.cur_sat_id[agent] = None

            # wait for overall clean

        next_video_chunk_sizes = []
        for i in range(BITRATE_LEVELS):
            next_video_chunk_sizes.append(self.video_size[i][self.video_chunk_counter[agent]])

        self.video_chunk_remain[agent] = video_chunk_remain
        self.download_bw[agent].append(float(video_chunk_size) / delay / M_IN_K * BITS_IN_BYTE)

        # num of users
        cur_sat_user_num = self.get_num_of_user_sat(self.cur_sat_id[agent])
        self.next_sat_id[agent] = next_sat_id
        next_sat_user_num = self.get_num_of_user_sat(next_sat_id)

        if model_type is not None and (agent == 0 or do_mpc):
            runner_up_sat_ids, ho_stamps, best_combos = self.run_mpc(agent, model_type)
        else:
            runner_up_sat_ids, ho_stamps, best_combos = None, None, None

        return delay, \
               sleep_time, \
               return_buffer_size / MILLISECONDS_IN_SECOND, \
               rebuf / MILLISECONDS_IN_SECOND, \
               video_chunk_size, \
               next_video_chunk_sizes, \
               self.end_of_video[agent], \
               video_chunk_remain, \
               is_handover, self.get_num_of_user_sat(sat_id="all"), \
               next_sat_bandwidth, next_sat_bw_logs, cur_sat_user_num, next_sat_user_num, cur_sat_bw_logs, connected_time, \
               self.cur_sat_id[agent], runner_up_sat_ids, ho_stamps, best_combos

    def get_video_chunk_oracle(self, quality, agent, mahimahi_ptr, ho_stamp, cur_sat_id, next_sat_id,
                               future_sat_user_nums):
        assert quality is not None

        video_chunk_counter = self.video_chunk_counter[agent]
        # use the delivery opportunity in mahimahi
        # delay = 0.0  # in ms
        # self.delay[agent] = 0
        end_of_network = False
        is_handover = False
        # cur_sat_id = self.cur_sat_id[agent]
        mahimahi_ptr = self.mahimahi_ptr[agent]
        last_mahimahi_time = self.last_mahimahi_time[agent]
        buffer_size = self.buffer_size[agent]

        cooked_bw = self.cooked_bw[agent]

        avg_bws = []
        rebuf = 0
        for idx, bitrate_level in enumerate(quality):
            video_chunk_size = self.video_size[bitrate_level][video_chunk_counter]
            delay = 0  # in ms
            video_chunk_counter_sent = 0  # in bytes

            if ho_stamp == idx:
                is_handover = True
                delay += HANDOVER_DELAY
                runner_up_sat_id, _ = self.get_runner_up_sat_id(agent, mahimahi_ptr=mahimahi_ptr,
                                                                method="harmonic-mean")
                self.update_sat_info(cur_sat_id, mahimahi_ptr, agent, -1)
                self.update_sat_info(runner_up_sat_id, mahimahi_ptr, agent, 1)

                cur_sat_id = runner_up_sat_id

            while True:  # download video chunk over mahimahi
                # num_of_users = 1 if future_sat_user_nums[cur_sat_id][idx] == 0 else future_sat_user_nums[cur_sat_id][idx]
                throughput = self.cur_satellite[self.cur_sat_id[agent]].data_rate(self.cur_user[agent],
                                                                                  self.mahimahi_ptr[agent])

                if throughput == 0.0:
                    # Do the forced handover
                    # Connect the satellite that has the best serving time
                    # runner_up_sat_id, _ = self.get_runner_up_sat_id(agent, method="harmonic-mean")
                    next_sat_id = self.get_best_sat_id(agent, mahimahi_ptr)

                    self.update_sat_info(cur_sat_id, mahimahi_ptr, agent, -1)
                    self.update_sat_info(next_sat_id, mahimahi_ptr, agent, 1)

                    cur_sat_id = next_sat_id
                    delay += HANDOVER_DELAY
                    if ho_stamp != MPC_FUTURE_CHUNK_COUNT:
                        return 100, 0
                    # self.download_bw[agent] = []
                    # print("Forced Handover")

                duration = self.cooked_time[mahimahi_ptr] \
                           - last_mahimahi_time

                packet_payload = throughput * duration * PACKET_PAYLOAD_PORTION

                if video_chunk_counter_sent + packet_payload > video_chunk_size:
                    fractional_time = (video_chunk_size - video_chunk_counter_sent) / \
                                      throughput / PACKET_PAYLOAD_PORTION
                    delay += fractional_time
                    last_mahimahi_time += fractional_time
                    break

                video_chunk_counter_sent += packet_payload
                delay += duration
                last_mahimahi_time = self.cooked_time[mahimahi_ptr]

                mahimahi_ptr += 1

                if mahimahi_ptr >= len(cooked_bw[cur_sat_id]):
                    # loop back in the beginning
                    # note: trace file starts with time 0
                    mahimahi_ptr = 1
                    last_mahimahi_time = 0
                    end_of_video = True
                    end_of_network = True
                    break

            delay *= MILLISECONDS_IN_SECOND
            delay += LINK_RTT

            # rebuffer time
            rebuf += np.maximum(delay - buffer_size, 0.0)
            # update the buffer
            buffer_size = np.maximum(buffer_size - delay, 0.0)

            # add in the new chunk
            buffer_size += VIDEO_CHUNCK_LEN

            # sleep if buffer gets too large
            sleep_time = 0
            if buffer_size > BUFFER_THRESH:
                drain_buffer_time = buffer_size - BUFFER_THRESH
                sleep_time = np.ceil(drain_buffer_time / DRAIN_BUFFER_SLEEP_TIME) * \
                             DRAIN_BUFFER_SLEEP_TIME
                buffer_size -= sleep_time

                while True:
                    if mahimahi_ptr >= len(cooked_bw[cur_sat_id]):
                        # loop back in the beginning
                        # note: trace file starts with time 0
                        mahimahi_ptr = 1
                        last_mahimahi_time = 0
                        end_of_video = True
                        end_of_network = True
                        break

                    duration = self.cooked_time[mahimahi_ptr] \
                               - last_mahimahi_time
                    if duration > sleep_time / MILLISECONDS_IN_SECOND:
                        last_mahimahi_time += sleep_time / MILLISECONDS_IN_SECOND
                        break
                    sleep_time -= duration * MILLISECONDS_IN_SECOND
                    last_mahimahi_time = self.cooked_time[mahimahi_ptr]
                    mahimahi_ptr += 1

                    throughput = cooked_bw[cur_sat_id][mahimahi_ptr] \
                                 * B_IN_MB / BITS_IN_BYTE / self.get_num_of_user_sat(cur_sat_id)

                    if throughput == 0.0:
                        # Do the forced handover
                        # Connect the satellite that has the best serving time
                        sat_id = self.get_best_sat_id(agent, mahimahi_ptr)
                        self.update_sat_info(cur_sat_id, mahimahi_ptr, agent, -1)
                        self.update_sat_info(sat_id, mahimahi_ptr, agent, 1)
                        prev_sat_id = cur_sat_id
                        cur_sat_id = sat_id
                        if ho_stamp != MPC_FUTURE_CHUNK_COUNT:
                            return 100, 0

            video_chunk_counter += 1
            avg_bws.append(float(video_chunk_size) / delay / M_IN_K * BITS_IN_BYTE)

            if video_chunk_counter >= TOTAL_VIDEO_CHUNCK or end_of_network:
                end_of_video = True
                buffer_size = 0
                video_chunk_counter = 0
                break

        return rebuf / MILLISECONDS_IN_SECOND, sum(avg_bws) / len(avg_bws)

    def get_video_chunk_oracle_v2(self, quality, agent, ho_stamp):
        assert quality is not None

        # use the delivery opportunity in mahimahi
        # delay = 0.0  # in ms
        # self.delay[agent] = 0
        end_of_network = False
        is_handover = False
        # cur_sat_id = self.cur_sat_id[agent]
        cooked_bw = self.cooked_bw[agent]

        avg_bws = []
        rebuf = 0
        video_chunk_size = self.video_size[quality][self.video_chunk_counter[agent]]
        delay = 0  # in ms
        video_chunk_counter_sent = 0  # in bytes

        if ho_stamp == 0:
            is_handover = True
            delay += HANDOVER_DELAY
            runner_up_sat_id, _ = self.get_runner_up_sat_id(agent, method="harmonic-mean")
            self.update_sat_info(self.cur_sat_id[agent], self.mahimahi_ptr[agent], agent, -1)
            self.update_sat_info(runner_up_sat_id, self.mahimahi_ptr[agent], agent, 1)

            self.cur_sat_id[agent] = runner_up_sat_id

        while True:  # download video chunk over mahimahi
            # num_of_users = 1 if future_sat_user_nums[cur_sat_id][idx] == 0 else future_sat_user_nums[cur_sat_id][idx]
            throughput = self.cur_satellite[self.cur_sat_id[agent]].data_rate(self.cur_user[agent],
                                                                              self.mahimahi_ptr[agent])

            if throughput == 0.0:
                # Do the forced handover
                # Connect the satellite that has the best serving time
                # runner_up_sat_id, _ = self.get_runner_up_sat_id(agent, method="harmonic-mean")
                next_sat_id = self.get_best_sat_id(agent, self.mahimahi_ptr[agent])

                self.update_sat_info(self.cur_sat_id[agent], self.mahimahi_ptr[agent], agent, -1)
                self.update_sat_info(next_sat_id, self.mahimahi_ptr[agent], agent, 1)

                self.cur_sat_id[agent] = next_sat_id
                delay += HANDOVER_DELAY
                # if ho_stamp != MPC_FUTURE_CHUNK_COUNT:
                #    return 100, 0
                # self.download_bw[agent] = []
                # print("Forced Handover")

            duration = self.cooked_time[self.mahimahi_ptr[agent]] \
                       - self.last_mahimahi_time[agent]

            packet_payload = throughput * duration * PACKET_PAYLOAD_PORTION

            if video_chunk_counter_sent + packet_payload > video_chunk_size:
                fractional_time = (video_chunk_size - video_chunk_counter_sent) / \
                                  throughput / PACKET_PAYLOAD_PORTION
                delay += fractional_time
                self.last_mahimahi_time[agent] += fractional_time
                break

            video_chunk_counter_sent += packet_payload
            delay += duration
            self.last_mahimahi_time[agent] = self.cooked_time[self.mahimahi_ptr[agent]]

            self.mahimahi_ptr[agent] += 1

            if self.mahimahi_ptr[agent] >= len(cooked_bw[self.cur_sat_id[agent]]):
                # loop back in the beginning
                # note: trace file starts with time 0
                self.mahimahi_ptr[agent] = 1
                self.last_mahimahi_time[agent] = 0
                end_of_video = True
                end_of_network = True
                break

        delay *= MILLISECONDS_IN_SECOND
        delay += LINK_RTT

        # rebuffer time
        rebuf = np.maximum(delay - self.buffer_size[agent], 0.0)
        # update the buffer
        self.buffer_size[agent] = np.maximum(self.buffer_size[agent] - delay, 0.0)

        # add in the new chunk
        self.buffer_size[agent] += VIDEO_CHUNCK_LEN

        # sleep if buffer gets too large
        sleep_time = 0
        if self.buffer_size[agent] > BUFFER_THRESH:
            drain_buffer_time = self.buffer_size[agent] - BUFFER_THRESH
            sleep_time = np.ceil(drain_buffer_time / DRAIN_BUFFER_SLEEP_TIME) * \
                         DRAIN_BUFFER_SLEEP_TIME
            self.buffer_size[agent] -= sleep_time

            while True:
                if self.mahimahi_ptr[agent] >= len(cooked_bw[self.cur_sat_id[agent]]):
                    # loop back in the beginning
                    # note: trace file starts with time 0
                    self.mahimahi_ptr[agent] = 1
                    self.last_mahimahi_time[agent] = 0
                    end_of_video = True
                    end_of_network = True
                    break

                duration = self.cooked_time[self.mahimahi_ptr[agent]] \
                           - self.last_mahimahi_time[agent]
                if duration > sleep_time / MILLISECONDS_IN_SECOND:
                    self.last_mahimahi_time[agent] += sleep_time / MILLISECONDS_IN_SECOND
                    break
                sleep_time -= duration * MILLISECONDS_IN_SECOND
                self.last_mahimahi_time[agent] = self.cooked_time[self.mahimahi_ptr[agent]]
                self.mahimahi_ptr[agent] += 1
                throughput = self.cur_satellite[self.cur_sat_id[agent]].data_rate(self.cur_user[agent],
                                                                                  self.mahimahi_ptr[agent])

                if throughput == 0.0:
                    # Do the forced handover
                    # Connect the satellite that has the best serving time
                    sat_id = self.get_best_sat_id(agent, self.mahimahi_ptr[agent])
                    self.update_sat_info(self.cur_sat_id[agent], self.mahimahi_ptr[agent], agent, -1)
                    self.update_sat_info(sat_id, self.mahimahi_ptr[agent], agent, 1)
                    prev_sat_id = self.cur_sat_id[agent]
                    self.cur_sat_id[agent] = sat_id
                    # if ho_stamp != MPC_FUTURE_CHUNK_COUNT:
                    #     return 100, 0

        self.video_chunk_counter[agent] += 1
        avg_bws.append(float(video_chunk_size) / delay / M_IN_K * BITS_IN_BYTE)

        if self.video_chunk_counter[agent] >= TOTAL_VIDEO_CHUNCK or end_of_network:
            end_of_video = True
            self.buffer_size[agent] = 0
            self.video_chunk_counter[agent] = 0

        return rebuf / MILLISECONDS_IN_SECOND, float(video_chunk_size) / delay / M_IN_K * BITS_IN_BYTE

    def reset(self):

        self.video_chunk_counter = [0 for _ in range(self.num_agents)]
        self.buffer_size = [0 for _ in range(self.num_agents)]
        self.video_chunk_counter_sent = [0 for _ in range(self.num_agents)]
        self.video_chunk_remain = [TOTAL_VIDEO_CHUNCK for _ in range(self.num_agents)]
        self.end_of_video = [False for _ in range(self.num_agents)]
        self.next_video_chunk_sizes = [[] for _ in range(self.num_agents)]
        # self.next_sat_bandwidth = [[] for _ in range(self.num_agents)]
        self.next_sat_id = [[] for _ in range(self.num_agents)]
        self.delay = [0 for _ in range(self.num_agents)]
        self.num_of_user_sat = {}
        self.download_bw = [[] for _ in range(self.num_agents)]
        self.cur_satellite = {}

        self.trace_idx += 1
        if self.trace_idx >= len(self.all_cooked_time):
            self.trace_idx = 0

        self.cooked_time = self.all_cooked_time[self.trace_idx]
        self.cooked_bw = self.all_cooked_bw[self.trace_idx]

        sat_list = []
        for user_id in range(self.num_agents):
            for sat_id, sat_bw in self.cooked_bw[user_id].items():
                if sat_id not in sat_list:
                    self.num_sat_info[sat_id] = [0 for _ in range(len(sat_bw))]
                    self.num_of_user_sat[sat_id] = 0
                    self.cur_satellite[sat_id] = Satellite(sat_id, sat_bw, user_id, SAT_STRATEGY)
                    sat_list.append(sat_id)
                else:
                    self.cur_satellite[sat_id].add_bw(sat_bw, user_id)

        self.cur_user = []
        for agent_id in range(self.num_agents):
            # self.cur_user.append(User(agent_id, SNR_MIN))
            self.cur_user.append(User(agent_id, None))
        self.mahimahi_ptr = [1 for _ in range(self.num_agents)]
        self.last_mahimahi_time = [self.cooked_time[self.mahimahi_start_ptr - 1] for _ in range(self.num_agents)]

        self.cur_sat_id = []
        for agent in range(self.num_agents):
            cur_sat_id = self.get_best_sat_id(agent)
            # self.connection[cur_sat_id] = agent
            self.cur_sat_id.append(cur_sat_id)
            self.update_sat_info(cur_sat_id, self.mahimahi_ptr[agent], agent, 1)

    def check_end(self):
        for agent in range(self.num_agents):
            if not self.end_of_video[agent]:
                return False
        return True

    def get_first_agent(self):
        user = -1

        for agent in range(self.num_agents):
            if not self.end_of_video[agent]:
                if user == -1:
                    user = agent
                else:
                    if self.last_mahimahi_time[agent] < self.last_mahimahi_time[user]:
                        user = agent

        return user

    def get_next_sat_info(self, agent, mahimahi_ptr=None):
        best_sat_id = None
        best_sat_bw = 0
        best_bw_list = []
        cur_sat_bw_list = []
        up_time_list = []
        if mahimahi_ptr is None:
            mahimahi_ptr = self.mahimahi_ptr[agent]
        cooked_bw = self.cooked_bw[agent]
        list1, list2, list3 = [], [], []
        bw_list = []
        sat_bw = cooked_bw[self.cur_sat_id[agent]]
        for i in range(MPC_PAST_CHUNK_COUNT, 0, -1):
            if mahimahi_ptr - i >= 0:
                if self.get_num_of_user_sat(self.cur_sat_id[agent]) == 0:
                    bw_list.append(sat_bw[mahimahi_ptr - i])
                else:
                    bw_list.append(sat_bw[mahimahi_ptr - i] / self.get_num_of_user_sat(self.cur_sat_id[agent]))
        if len(bw_list) == 0:
            bw = 0
        else:
            bw = sum(bw_list) / len(bw_list)
        up_time = 0
        tmp_index = mahimahi_ptr - 1
        tmp_sat_bw = sat_bw[tmp_index]
        while tmp_sat_bw != 0 and tmp_index >= 0:
            up_time += 1
            tmp_index -= 1
            tmp_sat_bw = sat_bw[tmp_index]
        up_time_list.append(up_time)
        list1.append(bw)
        cur_sat_bw_list = bw_list

        for sat_id, sat_bw in cooked_bw.items():
            bw_list = []
            if sat_id == self.cur_sat_id[agent]:
                continue
            for i in range(MPC_PAST_CHUNK_COUNT, 0, -1):
                if mahimahi_ptr - i >= 0 and sat_bw[mahimahi_ptr - i] != 0:
                    if self.get_num_of_user_sat(sat_id) == 0:
                        bw_list.append(sat_bw[mahimahi_ptr - i])
                    else:
                        bw_list.append(sat_bw[mahimahi_ptr - i] / (self.get_num_of_user_sat(sat_id) + 1))
            if len(bw_list) == 0:
                continue
            bw = sum(bw_list) / len(bw_list)
            if best_sat_bw < bw:
                best_sat_id = sat_id
                best_sat_bw = bw
                best_bw_list = bw_list

        if best_sat_id is None:
            best_sat_id = self.cur_sat_id[agent]

        up_time = 0
        tmp_index = mahimahi_ptr - 1
        sat_bw = cooked_bw[best_sat_id]
        tmp_sat_bw = sat_bw[tmp_index]
        while tmp_sat_bw != 0 and tmp_index >= 0:
            up_time += 1
            tmp_index -= 1
            tmp_sat_bw = sat_bw[tmp_index]
        up_time_list.append(up_time)

        list1.append(best_sat_bw)
        list2 = best_sat_id
        list3 = best_bw_list
        # zipped_lists = zip(list1, list2)
        # sorted_pairs = sorted(zipped_lists)

        # tuples = zip(*sorted_pairs)
        # list1, list2 = [ list(tuple) for tuple in  tuples]
        # list1 = [ list1[i] for i in range(1)]
        # list2 = [ list2[i] for i in range(1)]

        return cur_sat_bw_list, list1, list2, list3, up_time_list

    def get_best_sat_id(self, agent, mahimahi_ptr=None):
        best_sat_id = None
        best_sat_bw = 0
        cooked_bw = self.cooked_bw[agent]

        if mahimahi_ptr is None:
            mahimahi_ptr = self.mahimahi_ptr[agent]


        for sat_id, sat_bw in cooked_bw.items():
            if self.get_num_of_user_sat(sat_id) == 0:
                real_sat_bw = sat_bw[mahimahi_ptr]
            else:
                real_sat_bw = sat_bw[mahimahi_ptr] / (self.get_num_of_user_sat(sat_id) + 1)
            if best_sat_bw < real_sat_bw:
                best_sat_id = sat_id
                best_sat_bw = real_sat_bw

        assert best_sat_id is not None
        return best_sat_id

    def switch_sat(self, agent, cur_sat_id):
        pre_sat_id = self.cur_sat_id[agent]
        self.prev_sat_id[agent] = pre_sat_id

        # self.connection[pre_sat_id] = -1
        # self.connection[cur_sat_id] = agent

        self.cur_sat_id[agent] = cur_sat_id

    def run_mpc(self, agent, model_type):
        if model_type == "ManifoldMPC":
            is_handover, new_sat_id, bit_rate = self.qoe_v2(
                agent, only_runner_up=False)
        elif model_type == "DualMPC":
            is_handover, new_sat_id, bit_rate = self.qoe_v2(
                agent, only_runner_up=True)
        elif model_type == "DualMPC-Centralization":
            is_handover, new_sat_id, bit_rate = self.qoe_v2(
                agent, centralized=True)
        elif model_type == "DualMPC-Centralization-Exhaustive":
            runner_up_sat_ids, ho_stamps, best_combos, max_rewards, best_user_info = self.qoe_v3(agent)
            if best_user_info:
                for sat_id in best_user_info:
                    self.cur_satellite[sat_id].set_data_rate_ratio(best_user_info[sat_id][2], best_user_info[sat_id][3])
        elif model_type == "DualMPC-Centralization-Reduced":
            runner_up_sat_ids, ho_stamps, best_combos, max_rewards, best_user_info = self.qoe_v3(agent, reduced=True)
            if best_user_info:
                for sat_id in best_user_info:
                    self.cur_satellite[sat_id].set_data_rate_ratio(best_user_info[sat_id][2], best_user_info[sat_id][3])

        else:
            print("Cannot happen!")
            exit(-1)
        return runner_up_sat_ids, ho_stamps, best_combos

    def qoe_v2(self, agent, only_runner_up=True, centralized=False):
        is_handover = False
        best_sat_id = self.cur_sat_id[agent]
        # start_time = time.time()
        ho_sat_id, ho_stamp, best_combo, max_reward = self.calculate_mpc_with_handover(
            agent, only_runner_up=only_runner_up, centralized=centralized)

        if ho_stamp == 0:
            is_handover = True
            best_sat_id = ho_sat_id
        # print(time.time() - start_time)
        bit_rate = best_combo[0]

        return is_handover, best_sat_id, bit_rate

    def qoe_v3(self, agent, reduced=False):
        is_handover = False
        best_sat_id = self.cur_sat_id[agent]
        # start_time = time.time()rewards
        best_user_info = None
        if reduced:
            if SAT_STRATEGY == "resource-fair":
                runner_up_sat_ids, ho_stamps, best_combos, max_rewards = self.calculate_mpc_with_handover_exhaustive_reduced(agent)
            else:
                runner_up_sat_ids, ho_stamps, best_combos, max_rewards, best_user_info = self.calculate_mpc_with_handover_exhaustive_ratio_reduced(
                    agent)
        else:
            # runner_up_sat_ids, ho_stamps, best_combos, max_rewards= self.calculate_mpc_with_handover_exhaustive_reduced(agent)
            if SAT_STRATEGY == "resource-fair":
                runner_up_sat_ids, ho_stamps, best_combos, max_rewards = self.calculate_mpc_with_handover_exhaustive(agent)
            else:
                runner_up_sat_ids, ho_stamps, best_combos, max_rewards, best_user_info = self.calculate_mpc_with_handover_exhaustive_ratio(
                    agent)

        # runner_up_sat_ids, ho_stamps, best_combos, max_rewards = self.calculate_mpc_with_handover_exhaustive_oracle(agent)

        # print(time.time()-start_time)
        # print(runner_up_sat_ids, ho_stamps, best_combos, max_rewards)
        return runner_up_sat_ids, ho_stamps, best_combos, max_rewards, best_user_info

    def calculate_mpc_with_handover_exhaustive_ratio(self, agent):
        # future chunks length (try 4 if that many remaining)
        video_chunk_remain = [self.video_chunk_remain[i] for i in range(self.num_agents)]
        # last_index = self.get_total_video_chunk() - video_chunk_remain

        chunk_combo_option = []
        ho_combo_option = []
        # make chunk combination options
        for combo in itertools.product(list(range(int(BITRATE_LEVELS / BITRATE_WEIGHT))),
                                       repeat=MPC_FUTURE_CHUNK_COUNT * self.num_agents):
            chunk_combo_option.append(list([BITRATE_WEIGHT * x for x in combo]))

        # make handover combination options
        for combo in itertools.product(list(range(MPC_FUTURE_CHUNK_COUNT + 1)), repeat=self.num_agents):
            ho_combo_option.append(list(combo))

        future_chunk_length = [MPC_FUTURE_CHUNK_COUNT] * self.num_agents
        for i in range(self.num_agents):
            if video_chunk_remain[i] < MPC_FUTURE_CHUNK_COUNT:
                future_chunk_length[i] = video_chunk_remain[i]

        cur_download_bws = [self.predict_download_bw(i, True) for i in range(self.num_agents)]

        cur_sat_ids = [self.cur_sat_id[i] for i in range(self.num_agents)]
        runner_up_sat_ids = [self.get_runner_up_sat_id(i, method="harmonic-mean")[0] for i in range(self.num_agents)]

        related_sat_ids = list(set(cur_sat_ids + runner_up_sat_ids))
        num_of_sats = {}
        for sat_id in related_sat_ids:
            num_of_sats[sat_id] = self.get_num_of_user_sat(sat_id)

        start_buffers = [self.buffer_size[i] / MILLISECONDS_IN_SECOND for i in range(self.num_agents)]

        next_download_bws = []
        for agent_id in range(self.num_agents):
            for i in range(MPC_PAST_CHUNK_COUNT, 0, -1):
                self.predict_bw(runner_up_sat_ids[agent_id], agent_id, True,
                                mahimahi_ptr=self.mahimahi_ptr[agent_id] - i,
                                plus=False)
                self.predict_bw(cur_sat_ids[agent_id], agent_id, True, mahimahi_ptr=self.mahimahi_ptr[agent_id] - i,
                                plus=False)

            tmp_next_bw = self.predict_bw(runner_up_sat_ids[agent_id], agent_id, True)
            tmp_cur_bw = self.predict_bw(cur_sat_ids[agent_id], agent_id, True)
            if cur_download_bws[agent_id] is None:
                next_download_bws.append(None)
            else:
                assert cur_download_bws[agent_id] * tmp_next_bw / tmp_cur_bw != 0.0
                next_download_bws.append(cur_download_bws[agent_id] * tmp_next_bw / tmp_cur_bw)

        max_rewards = [-10000000 for _ in range(self.num_agents)]
        best_combos = [[self.last_quality[i]] * MPC_FUTURE_CHUNK_COUNT for i in range(self.num_agents)]
        best_bws_sum = [-10000000]
        ho_stamps = [MPC_FUTURE_CHUNK_COUNT for _ in range(self.num_agents)]
        sat_user_nums = num_of_sats
        best_user_info = None

        for ho_positions in ho_combo_option:
            future_sat_user_nums = {}
            future_sat_user_list = {}

            for sat_id in sat_user_nums.keys():
                future_sat_user_nums[sat_id] = np.array([sat_user_nums[sat_id]] * MPC_FUTURE_CHUNK_COUNT)
                future_sat_user_list[sat_id] = {}
                for i in range(MPC_FUTURE_CHUNK_COUNT):
                    future_sat_user_list[sat_id][i] = copy.deepcopy(self.cur_satellite[sat_id].get_ue_list())

            for idx, ho_point in enumerate(ho_positions):
                cur_sat_id = cur_sat_ids[idx]
                next_sat_id = runner_up_sat_ids[idx]
                cur_nums = future_sat_user_nums[cur_sat_id]
                next_nums = future_sat_user_nums[next_sat_id]

                cur_nums[ho_point:] = cur_nums[ho_point:] - 1
                next_nums[ho_point:] = next_nums[ho_point:] + 1

                for i in range(MPC_FUTURE_CHUNK_COUNT):
                    if i >= ho_point:
                        future_sat_user_list[cur_sat_id][i].remove(idx)
                        future_sat_user_list[next_sat_id][i].append(idx)

                future_sat_user_nums[cur_sat_id] = cur_nums
                future_sat_user_nums[next_sat_id] = next_nums

            for full_combo in chunk_combo_option:
                combos = []
                # Break at the end of the chunk

                for agent_id in range(self.num_agents):
                    cur_combo = full_combo[MPC_FUTURE_CHUNK_COUNT * agent_id:
                                           MPC_FUTURE_CHUNK_COUNT * agent_id + future_chunk_length[agent_id]]
                    # if cur_download_bws[agent_id] is None and cur_combo != [DEFAULT_QUALITY] * MPC_FUTURE_CHUNK_COUNT:
                    #     wrong_format = True
                    #     break
                    if cur_download_bws[agent_id] is None:
                        combos.append([np.nan] * MPC_FUTURE_CHUNK_COUNT)
                    else:
                        combos.append(cur_combo)

                bw_ratio = {}
                user_info = {}
                op_vars = []
                op_vars_index = 0
                bounds = []
                constraints = []
                sat_id_list = []
                const_array = []
                for sat_id in future_sat_user_nums.keys():
                    user_list = []
                    is_multi_users = False
                    for i in range(len(future_sat_user_nums[sat_id])):
                        if future_sat_user_nums[sat_id][i] > 1:
                            is_multi_users = True
                            user_list = [*user_list, *future_sat_user_list[sat_id][i]]
                    if is_multi_users:
                        user_list = list(set(user_list))
                        user_info[sat_id] = (op_vars_index, op_vars_index + len(user_list), user_list)

                        op_vars = [*op_vars, *([1 / len(user_list)] * len(user_list))]
                        bounds = [*bounds, *[(0 + EPSILON, 1 - EPSILON) for _ in range(len(user_list))]]
                        sat_id_list.append(sat_id)

                        target_array = np.zeros(op_vars_index + len(user_list))
                        target_array[op_vars_index:op_vars_index+len(user_list)] = 1
                        op_vars_index += len(user_list)

                        const_array.append(target_array)

                if op_vars:
                    for i in range(len(const_array)):
                        data = const_array[i]
                        if len(const_array[i]) < op_vars_index:
                            data = np.append(const_array[i], [0] * (op_vars_index - len(const_array[i])))

                        constraint = LinearConstraint(data, lb=1, ub=1)

                        # constraints = [*constraints, {'type': 'eq', 'fun': const}]
                        constraints.append(constraint)

                    import warnings
                    warnings.filterwarnings("ignore")
                    ue_ratio = minimize(
                        self.objective_function,
                        x0=np.array(op_vars),
                        args=(combos, cur_sat_ids, runner_up_sat_ids, sat_user_nums,
                              future_sat_user_nums, ho_positions, start_buffers,
                              video_chunk_remain, cur_download_bws,
                              next_download_bws, user_info, bw_ratio),
                        constraints=constraints,
                        bounds=bounds,
                        method="SLSQP"  # or BFGS
                    )
                    for sat_id in sat_id_list:
                        user_info[sat_id] = user_info[sat_id] + (ue_ratio.x[user_info[sat_id][0]:user_info[sat_id][1]],)
                        # user_info[sat_id] = user_info[sat_id] + (np.array([0.5, 0.5]),)

                rewards = []
                tmp_bws_sum = []
                for agent_id, combo in enumerate(combos):
                    if combo == [np.nan] * MPC_FUTURE_CHUNK_COUNT:
                        rewards.append(np.nan)
                        continue
                    curr_rebuffer_time = 0
                    curr_buffer = start_buffers[agent_id]
                    bitrate_sum = 0
                    smoothness_diff = 0
                    last_quality = self.last_quality[agent_id]
                    last_index = int(CHUNK_TIL_VIDEO_END_CAP - video_chunk_remain[agent_id])

                    for position in range(0, len(combo)):
                        chunk_quality = combo[position]
                        index = last_index + position  # e.g., if last chunk is 3, then first iter is 3+0+1=4
                        download_time = 0

                        cur_sat_id = cur_sat_ids[agent_id]
                        next_sat_id = runner_up_sat_ids[agent_id]

                        cur_sat_user_num = 1 if sat_user_nums[cur_sat_id] == 0 \
                            else sat_user_nums[cur_sat_id]
                        next_sat_user_num = 1 if sat_user_nums[next_sat_id] == 0 \
                            else sat_user_nums[next_sat_id]

                        # cur_sat_user_num = sat_user_nums[cur_sat_id]
                        cur_future_sat_user_num = future_sat_user_nums[cur_sat_id][position]
                        # next_sat_user_num = sat_user_nums[next_sat_id]
                        next_future_sat_user_num = future_sat_user_nums[next_sat_id][position]

                        now_sat_id = None
                        if ho_positions[agent_id] > position:
                            harmonic_bw = cur_download_bws[agent_id] * cur_sat_user_num
                            if cur_future_sat_user_num > 1:
                                now_sat_id = cur_sat_id
                        elif ho_positions[agent_id] == position:
                            harmonic_bw = next_download_bws[agent_id] * next_sat_user_num
                            if next_future_sat_user_num > 1:
                                now_sat_id = next_sat_id
                            # Give them a penalty
                            download_time += HANDOVER_DELAY
                        else:
                            harmonic_bw = next_download_bws[agent_id] * next_sat_user_num
                            if next_future_sat_user_num > 1:
                                now_sat_id = next_sat_id

                        if now_sat_id:
                            var_index = user_info[now_sat_id][2].index(agent_id)
                            harmonic_bw *= user_info[now_sat_id][3][var_index]

                        tmp_bws_sum.append(harmonic_bw)

                        download_time += (self.video_size[chunk_quality][index] / B_IN_MB) \
                                         / harmonic_bw * BITS_IN_BYTE  # this is MB/MB/s --> seconds

                        if curr_buffer < download_time:
                            curr_rebuffer_time += (download_time - curr_buffer)
                            curr_buffer = 0.0
                        else:
                            curr_buffer -= download_time
                        curr_buffer += VIDEO_CHUNCK_LEN / MILLISECONDS_IN_SECOND

                        # bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                        # smoothness_diffs += abs(VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                        bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                        smoothness_diff += abs(
                            VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                        last_quality = chunk_quality
                    # compute reward for this combination (one reward per 5-chunk combo)

                    # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

                    # 10~140 - 0~100 - 0~130
                    rewards.append(bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * curr_rebuffer_time) \
                                   - SMOOTH_PENALTY * smoothness_diff / M_IN_K)

                if np.nanmean(rewards) > np.nanmean(max_rewards):
                    best_combos = combos
                    max_rewards = rewards
                    ho_stamps = ho_positions
                    best_bws_sum = tmp_bws_sum
                    best_user_info = user_info
                elif np.nanmean(rewards) == np.nanmean(max_rewards) and \
                        (np.nanmean(tmp_bws_sum) >= np.nanmean(best_bws_sum)):
                    # elif np.nanmean(rewards) == np.nanmean(max_rewards) \
                    #         and (rewards[agent] >= max_rewards[agent] or combos[agent][0] >= best_combos[agent][0]):
                    best_combos = combos
                    max_rewards = rewards
                    ho_stamps = ho_positions
                    best_bws_sum = tmp_bws_sum
                    best_user_info = user_info
        # return runner_up_sat_ids[agent], ho_stamps[agent], best_combos[agent], max_rewards[agent]
        # print(best_user_info)
        return runner_up_sat_ids, ho_stamps, best_combos, max_rewards, best_user_info

    def calculate_mpc_with_handover_exhaustive(self, agent):
        # future chunks length (try 4 if that many remaining)
        video_chunk_remain = [self.video_chunk_remain[i] for i in range(self.num_agents)]
        # last_index = self.get_total_video_chunk() - video_chunk_remain

        chunk_combo_option = []
        ho_combo_option = []
        # make chunk combination options
        for combo in itertools.product(list(range(int(BITRATE_LEVELS / BITRATE_WEIGHT))),
                                       repeat=MPC_FUTURE_CHUNK_COUNT * self.num_agents):
            chunk_combo_option.append(list([BITRATE_WEIGHT * x for x in combo]))

        # make handover combination options
        for combo in itertools.product(list(range(MPC_FUTURE_CHUNK_COUNT + 1)), repeat=self.num_agents):
            ho_combo_option.append(list(combo))

        future_chunk_length = [MPC_FUTURE_CHUNK_COUNT] * self.num_agents
        for i in range(self.num_agents):
            if video_chunk_remain[i] < MPC_FUTURE_CHUNK_COUNT:
                future_chunk_length[i] = video_chunk_remain[i]

        cur_download_bws = [self.predict_download_bw(i, True) for i in range(self.num_agents)]

        cur_sat_ids = [self.cur_sat_id[i] for i in range(self.num_agents)]
        runner_up_sat_ids = [self.get_runner_up_sat_id(i, method="harmonic-mean")[0] for i in range(self.num_agents)]

        related_sat_ids = list(set(cur_sat_ids + runner_up_sat_ids))
        num_of_sats = {}
        for sat_id in related_sat_ids:
            num_of_sats[sat_id] = self.get_num_of_user_sat(sat_id)

        start_buffers = [self.buffer_size[i] / MILLISECONDS_IN_SECOND for i in range(self.num_agents)]

        next_download_bws = []
        for agent_id in range(self.num_agents):
            for i in range(MPC_PAST_CHUNK_COUNT, 0, -1):
                self.predict_bw(runner_up_sat_ids[agent_id], agent_id, True,
                                mahimahi_ptr=self.mahimahi_ptr[agent_id] - i,
                                plus=False)
                self.predict_bw(cur_sat_ids[agent_id], agent_id, True, mahimahi_ptr=self.mahimahi_ptr[agent_id] - i,
                                plus=False)

            tmp_next_bw = self.predict_bw(runner_up_sat_ids[agent_id], agent_id, True)
            tmp_cur_bw = self.predict_bw(cur_sat_ids[agent_id], agent_id, True)
            if cur_download_bws[agent_id] is None:
                next_download_bws.append(None)
            else:
                assert cur_download_bws[agent_id] * tmp_next_bw / tmp_cur_bw != 0.0
                next_download_bws.append(cur_download_bws[agent_id] * tmp_next_bw / tmp_cur_bw)

        max_rewards = [-10000000 for _ in range(self.num_agents)]
        best_combos = [[self.last_quality[i]] * MPC_FUTURE_CHUNK_COUNT for i in range(self.num_agents)]
        ho_stamps = [MPC_FUTURE_CHUNK_COUNT for _ in range(self.num_agents)]
        best_bws = [-10000000 for _ in range(self.num_agents)]
        best_bws_sum = [-10000000]

        sat_user_nums = num_of_sats

        for ho_positions in ho_combo_option:
            future_sat_user_nums = {}
            tmp_bws = []
            tmp_bws_sum = []

            for sat_id in sat_user_nums.keys():
                future_sat_user_nums[sat_id] = np.array([sat_user_nums[sat_id]] * MPC_FUTURE_CHUNK_COUNT)

            for idx, ho_point in enumerate(ho_positions):
                cur_sat_id = cur_sat_ids[idx]
                next_sat_id = runner_up_sat_ids[idx]
                cur_nums = future_sat_user_nums[cur_sat_id]
                next_nums = future_sat_user_nums[next_sat_id]

                cur_nums[ho_point:] = cur_nums[ho_point:] - 1
                next_nums[ho_point:] = next_nums[ho_point:] + 1

                future_sat_user_nums[cur_sat_id] = cur_nums
                future_sat_user_nums[next_sat_id] = next_nums

            for full_combo in chunk_combo_option:
                combos = []
                # Break at the end of the chunk

                for agent_id in range(self.num_agents):
                    cur_combo = full_combo[MPC_FUTURE_CHUNK_COUNT * agent_id:
                                           MPC_FUTURE_CHUNK_COUNT * agent_id + future_chunk_length[agent_id]]
                    # if cur_download_bws[agent_id] is None and cur_combo != [DEFAULT_QUALITY] * MPC_FUTURE_CHUNK_COUNT:
                    #     wrong_format = True
                    #     break
                    if cur_download_bws[agent_id] is None:
                        combos.append([np.nan] * MPC_FUTURE_CHUNK_COUNT)
                    else:
                        combos.append(cur_combo)

                rewards = []
                tmp_bws_sum = []
                for agent_id, combo in enumerate(combos):
                    if combo == [np.nan] * MPC_FUTURE_CHUNK_COUNT:
                        rewards.append(np.nan)
                        continue
                    curr_rebuffer_time = 0
                    curr_buffer = start_buffers[agent_id]
                    bitrate_sum = 0
                    smoothness_diff = 0
                    last_quality = self.last_quality[agent_id]
                    last_index = int(CHUNK_TIL_VIDEO_END_CAP - video_chunk_remain[agent_id])

                    cur_sat_id = cur_sat_ids[agent_id]
                    next_sat_id = runner_up_sat_ids[agent_id]

                    cur_sat_user_num = 1 if sat_user_nums[cur_sat_id] == 0 \
                        else sat_user_nums[cur_sat_id]
                    next_sat_user_num = 1 if sat_user_nums[next_sat_id] == 0 \
                        else sat_user_nums[next_sat_id]

                    for position in range(0, len(combo)):
                        chunk_quality = combo[position]
                        index = last_index + position  # e.g., if last chunk is 3, then first iter is 3+0+1=4
                        download_time = 0

                        # cur_sat_user_num = sat_user_nums[cur_sat_id]
                        cur_future_sat_user_num = future_sat_user_nums[cur_sat_id][position]
                        # next_sat_user_num = sat_user_nums[next_sat_id]
                        next_future_sat_user_num = future_sat_user_nums[next_sat_id][position]

                        if ho_positions[agent_id] > position:
                            harmonic_bw = cur_download_bws[agent_id] * cur_sat_user_num / cur_future_sat_user_num
                        elif ho_positions[agent_id] == position:
                            harmonic_bw = next_download_bws[agent_id] * next_sat_user_num / next_future_sat_user_num
                            # Give them a penalty
                            download_time += HANDOVER_DELAY

                        else:
                            harmonic_bw = next_download_bws[agent_id] * next_sat_user_num / next_future_sat_user_num

                        tmp_bws_sum.append(harmonic_bw)

                        download_time += (self.video_size[chunk_quality][index] / B_IN_MB) \
                                         / harmonic_bw * BITS_IN_BYTE  # this is MB/MB/s --> seconds

                        if curr_buffer < download_time:
                            curr_rebuffer_time += (download_time - curr_buffer)
                            curr_buffer = 0.0
                        else:
                            curr_buffer -= download_time
                        curr_buffer += VIDEO_CHUNCK_LEN / MILLISECONDS_IN_SECOND

                        # bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                        # smoothness_diffs += abs(VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                        bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                        smoothness_diff += abs(
                            VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                        last_quality = chunk_quality
                    # compute reward for this combination (one reward per 5-chunk combo)

                    # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

                    # 10~140 - 0~100 - 0~130
                    rewards.append(bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * curr_rebuffer_time) \
                                   - SMOOTH_PENALTY * smoothness_diff / M_IN_K)

                if np.nanmean(rewards) > np.nanmean(max_rewards):
                    best_combos = combos
                    max_rewards = rewards
                    ho_stamps = ho_positions
                    best_bws_sum = tmp_bws_sum
                elif np.nanmean(rewards) == np.nanmean(max_rewards) and \
                        (ho_stamps[agent] <= ho_positions[agent]
                         or np.nansum(combos[agent]) >= np.nansum(best_combos[agent])
                         or np.nanmean(tmp_bws_sum) >= np.nanmean(best_bws_sum)):
                    # (rewards[agent] >= max_rewards[agent] or combos[agent][0] >= best_combos[agent][0]):
                    best_combos = combos
                    max_rewards = rewards
                    ho_stamps = ho_positions
                    best_bws_sum = tmp_bws_sum

        # return runner_up_sat_ids[agent], ho_stamps[agent], best_combos[agent], max_rewards[agent]

        return runner_up_sat_ids, ho_stamps, best_combos, max_rewards

    def calculate_mpc_with_handover_exhaustive_oracle(self, agent):
        # future chunks length (try 4 if that many remaining)
        video_chunk_remain = [self.video_chunk_remain[i] for i in range(self.num_agents)]
        # last_index = self.get_total_video_chunk() - video_chunk_remain

        chunk_combo_option = []
        ho_combo_option = []
        # make chunk combination options
        for combo in itertools.product(list(range(int(BITRATE_LEVELS / BITRATE_WEIGHT))),
                                       repeat=MPC_FUTURE_CHUNK_COUNT * self.num_agents):
            chunk_combo_option.append(list([BITRATE_WEIGHT * x for x in combo]))

        # make handover combination options
        for combo in itertools.product(list(range(MPC_FUTURE_CHUNK_COUNT + 1)), repeat=self.num_agents):
            ho_combo_option.append(list(combo))

        future_chunk_length = [MPC_FUTURE_CHUNK_COUNT] * self.num_agents
        for i in range(self.num_agents):
            if video_chunk_remain[i] < MPC_FUTURE_CHUNK_COUNT:
                future_chunk_length[i] = video_chunk_remain[i]

        # cur_download_bws = [self.predict_download_bw(i, True) for i in range(self.num_agents)]

        cur_sat_ids = [self.cur_sat_id[i] for i in range(self.num_agents)]
        runner_up_sat_ids = [self.get_runner_up_sat_id(i, method="harmonic-mean")[0] for i in range(self.num_agents)]

        # related_sat_ids = list(set(cur_sat_ids + runner_up_sat_ids))
        num_of_sats = self.get_num_of_user_sat(sat_id="all")

        max_rewards = [-10000000 for _ in range(self.num_agents)]
        best_combos = [[self.last_quality[i] * MPC_FUTURE_CHUNK_COUNT] for i in range(self.num_agents)]
        best_bws_sum = [-10000000]
        ho_stamps = [MPC_FUTURE_CHUNK_COUNT for _ in range(self.num_agents)]
        sat_user_nums = copy.deepcopy(num_of_sats)

        for ho_positions in ho_combo_option:
            future_sat_user_nums = {}

            for sat_id in sat_user_nums.keys():
                future_sat_user_nums[sat_id] = np.array([sat_user_nums[sat_id]] * MPC_FUTURE_CHUNK_COUNT)

            for idx, ho_point in enumerate(ho_positions):
                cur_sat_id = cur_sat_ids[idx]
                next_sat_id = runner_up_sat_ids[idx]
                cur_nums = future_sat_user_nums[cur_sat_id]
                next_nums = future_sat_user_nums[next_sat_id]

                cur_nums[ho_point:] = cur_nums[ho_point:] - 1
                next_nums[ho_point:] = next_nums[ho_point:] + 1

                future_sat_user_nums[cur_sat_id] = cur_nums
                future_sat_user_nums[next_sat_id] = next_nums

            for full_combo in chunk_combo_option:
                combos = []
                # Break at the end of the chunk
                for agent_id in range(self.num_agents):
                    cur_combo = full_combo[MPC_FUTURE_CHUNK_COUNT * agent_id: MPC_FUTURE_CHUNK_COUNT * agent_id +
                                                                              future_chunk_length[agent_id]]
                    if not cur_combo:
                        combos.append([np.nan] * MPC_FUTURE_CHUNK_COUNT)
                    else:
                        combos.append(cur_combo)

                rewards = []
                tmp_bws_sum = []
                self.froze_num_of_user_sat()
                combo_log = copy.deepcopy(combos)
                ho_stamps_log = copy.deepcopy(ho_positions)
                last_quality = copy.deepcopy(self.last_quality)
                bitrate_sum = 0
                smoothness_diff = 0
                rebuf_time = 0
                while True:
                    cur_agent_id = self.get_first_agent()
                    if not combo_log[cur_agent_id]:
                        break
                    bit_rate = combo_log[cur_agent_id].pop(0)

                    ho_point = ho_stamps_log[cur_agent_id]
                    ho_stamps_log[cur_agent_id] -= 1
                    if np.isnan(bit_rate):
                        # bit_rate = DEFAULT_QUALITY
                        rewards.append(np.nan)
                        break
                        # continue

                    rebuf, avg_bw = self.get_video_chunk_oracle_v2(bit_rate, cur_agent_id, ho_point)
                    tmp_bws_sum.append(avg_bw)

                    bitrate_sum += VIDEO_BIT_RATE[bit_rate]
                    smoothness_diff += abs(
                        VIDEO_BIT_RATE[bit_rate] - VIDEO_BIT_RATE[last_quality[cur_agent_id]])
                    last_quality[cur_agent_id] = bit_rate
                    rebuf_time += rebuf

                rewards.append(bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * rebuf_time) \
                               - SMOOTH_PENALTY * smoothness_diff / M_IN_K)

                """
                for agent_id, combo in enumerate(combos):
                    if combo == [np.nan] * MPC_FUTURE_CHUNK_COUNT:
                        rewards.append(np.nan)
                        continue
                    bitrate_sum = 0
                    smoothness_diff = 0
                    last_quality = self.last_quality[agent_id]

                    cur_sat_id = cur_sat_ids[agent_id]
                    next_sat_id = runner_up_sat_ids[agent_id]
                    rebuf, avg_bw = self.get_video_chunk_oracle(combo, agent_id, self.mahimahi_ptr[agent_id], ho_positions[agent_id], cur_sat_id, next_sat_id, future_sat_user_nums)
                    tmp_bws_sum.append(avg_bw)
                    for position in range(0, len(combo)):
                        chunk_quality = combo[position]
                        bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                        smoothness_diff += abs(
                            VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                        last_quality = chunk_quality

                    rewards.append(bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * rebuf) \
                                   - SMOOTH_PENALTY * smoothness_diff / M_IN_K)
                """
                self.restore_num_of_user_sat()
                if np.nanmean(rewards) > np.nanmean(max_rewards):
                    best_combos = combos
                    max_rewards = rewards
                    ho_stamps = ho_positions
                    best_bws_sum = tmp_bws_sum
                elif np.nanmean(rewards) == np.nanmean(max_rewards) and \
                        (ho_stamps[agent] <= ho_positions[agent]
                         or np.nanmean(tmp_bws_sum) >= np.nanmean(best_bws_sum)):
                    # elif np.nanmean(rewards) == np.nanmean(max_rewards) \
                    #         and (rewards[agent] >= max_rewards[agent] or combos[agent][0] >= best_combos[agent][0]):
                    best_combos = combos
                    max_rewards = rewards
                    ho_stamps = ho_positions
                    best_bws_sum = tmp_bws_sum

        # return runner_up_sat_ids[agent], ho_stamps[agent], best_combos[agent], max_rewards[agent]
        # print(best_combos, max_rewards, ho_stamps)
        return runner_up_sat_ids, ho_stamps, best_combos, max_rewards

    def calculate_mpc_with_handover_exhaustive_reduced(self, agent):
        # future chunks length (try 4 if that many remaining)
        video_chunk_remain = [self.video_chunk_remain[i] for i in range(self.num_agents)]
        # last_index = self.get_total_video_chunk() - video_chunk_remain

        chunk_combo_option = []
        ho_combo_option = []
        # make chunk combination options
        for combo in itertools.product(list(range(int(BITRATE_LEVELS / BITRATE_WEIGHT))),
                                       repeat=MPC_FUTURE_CHUNK_COUNT * self.num_agents):
            chunk_combo_option.append(list([BITRATE_WEIGHT * x for x in combo]))

        # make handover combination options
        for combo in itertools.product(list(range(MPC_FUTURE_CHUNK_COUNT + 1)), repeat=self.num_agents):
            ho_combo_option.append(list(combo))

        future_chunk_length = [MPC_FUTURE_CHUNK_COUNT] * self.num_agents
        for i in range(self.num_agents):
            if video_chunk_remain[i] < MPC_FUTURE_CHUNK_COUNT:
                future_chunk_length[i] = video_chunk_remain[i]

        cur_download_bws = [self.predict_download_bw(i, True) for i in range(self.num_agents)]

        cur_sat_ids = [self.cur_sat_id[i] for i in range(self.num_agents)]
        runner_up_sat_ids = [self.get_runner_up_sat_id(i, method="harmonic-mean")[0] for i in range(self.num_agents)]

        related_sat_ids = list(set(cur_sat_ids + runner_up_sat_ids))
        num_of_sats = {}
        for sat_id in related_sat_ids:
            num_of_sats[sat_id] = self.get_num_of_user_sat(sat_id)

        start_buffers = [self.buffer_size[i] / MILLISECONDS_IN_SECOND for i in range(self.num_agents)]

        next_download_bws = []
        for agent_id in range(self.num_agents):
            for i in range(MPC_PAST_CHUNK_COUNT, 0, -1):
                self.predict_bw(runner_up_sat_ids[agent_id], agent_id, True,
                                mahimahi_ptr=self.mahimahi_ptr[agent_id] - i,
                                plus=False)
                self.predict_bw(cur_sat_ids[agent_id], agent_id, True, mahimahi_ptr=self.mahimahi_ptr[agent_id] - i,
                                plus=False)

            tmp_next_bw = self.predict_bw(runner_up_sat_ids[agent_id], agent_id, True)
            tmp_cur_bw = self.predict_bw(cur_sat_ids[agent_id], agent_id, True)
            if cur_download_bws[agent_id] is None:
                next_download_bws.append(None)
            else:
                assert cur_download_bws[agent_id] * tmp_next_bw / tmp_cur_bw != 0.0
                next_download_bws.append(cur_download_bws[agent_id] * tmp_next_bw / tmp_cur_bw)

        max_rewards = [-10000000 for _ in range(self.num_agents)]
        best_combos = [[self.last_quality[i]] for i in range(self.num_agents)]
        best_bws = [[-10000000] for _ in range(self.num_agents)]
        best_bws_sum = [-10000000]
        best_ho_positions = {}

        sat_user_nums = num_of_sats

        future_sat_user_nums = None

        for ho_positions in ho_combo_option:
            tmp_future_sat_user_nums = {}
            tmp_bws = []
            tmp_bws_sum = []

            for sat_id in sat_user_nums.keys():
                tmp_future_sat_user_nums[sat_id] = np.array([sat_user_nums[sat_id]] * MPC_FUTURE_CHUNK_COUNT)

            for idx, ho_point in enumerate(ho_positions):
                cur_sat_id = cur_sat_ids[idx]
                next_sat_id = runner_up_sat_ids[idx]
                cur_nums = tmp_future_sat_user_nums[cur_sat_id]
                next_nums = tmp_future_sat_user_nums[next_sat_id]

                cur_nums[ho_point:] = cur_nums[ho_point:] - 1
                next_nums[ho_point:] = next_nums[ho_point:] + 1

                tmp_future_sat_user_nums[cur_sat_id] = cur_nums
                tmp_future_sat_user_nums[next_sat_id] = next_nums

            for idx in range(self.num_agents):
                if cur_download_bws[idx] is None:
                    tmp_bws.append([np.nan])
                    tmp_bws_sum.append(np.nan)
                    continue

                cur_sat_id = cur_sat_ids[idx]
                next_sat_id = runner_up_sat_ids[idx]

                cur_sat_user_num = 1 if sat_user_nums[cur_sat_id] == 0 \
                    else sat_user_nums[cur_sat_id]
                next_sat_user_num = 1 if sat_user_nums[next_sat_id] == 0 \
                    else sat_user_nums[next_sat_id]
                bw_log = []
                for position in range(MPC_FUTURE_CHUNK_COUNT):
                    # cur_sat_user_num = sat_user_nums[cur_sat_id]
                    cur_future_sat_user_num = tmp_future_sat_user_nums[cur_sat_id][position]
                    # next_sat_user_num = sat_user_nums[next_sat_id]
                    next_future_sat_user_num = tmp_future_sat_user_nums[next_sat_id][position]

                    if ho_positions[idx] > position:
                        harmonic_bw = cur_download_bws[idx] * cur_sat_user_num / cur_future_sat_user_num
                    elif ho_positions[idx] == position:
                        harmonic_bw = next_download_bws[idx] * next_sat_user_num / next_future_sat_user_num
                    else:
                        harmonic_bw = next_download_bws[idx] * next_sat_user_num / next_future_sat_user_num

                    bw_log.append(harmonic_bw)
                    tmp_bws_sum.append(harmonic_bw)
                tmp_bws.append(bw_log)

            if np.nanmean(best_bws_sum) < np.nanmean(tmp_bws_sum):
                best_bws = tmp_bws
                best_ho_positions = ho_positions
                best_bws_sum = tmp_bws_sum
                future_sat_user_nums = tmp_future_sat_user_nums
            elif np.nanmean(best_bws_sum) == np.nanmean(tmp_bws_sum) and best_ho_positions[agent] <= ho_positions[
                agent]:
                best_bws = tmp_bws
                best_ho_positions = ho_positions
                best_bws_sum = tmp_bws_sum
                future_sat_user_nums = tmp_future_sat_user_nums

        for full_combo in chunk_combo_option:
            combos = []
            # Break at the end of the chunk

            for agent_id in range(self.num_agents):
                cur_combo = full_combo[
                            MPC_FUTURE_CHUNK_COUNT * agent_id:
                            MPC_FUTURE_CHUNK_COUNT * agent_id + future_chunk_length[agent_id]]
                # if cur_download_bws[agent_id] is None and cur_combo != [DEFAULT_QUALITY] * MPC_FUTURE_CHUNK_COUNT:
                #     wrong_format = True
                #     break
                if cur_download_bws[agent_id] is None:
                    combos.append([np.nan] * MPC_FUTURE_CHUNK_COUNT)
                else:
                    combos.append(cur_combo)

            rewards = []
            for agent_id, combo in enumerate(combos):
                if combo == [np.nan] * MPC_FUTURE_CHUNK_COUNT:
                    rewards.append(np.nan)
                    continue
                curr_rebuffer_time = 0
                curr_buffer = start_buffers[agent_id]
                bitrate_sum = 0
                smoothness_diff = 0
                last_quality = self.last_quality[agent_id]
                last_index = int(CHUNK_TIL_VIDEO_END_CAP - video_chunk_remain[agent_id])
                # linear optimization
                # constraint = LinearConstraint(np.ones(self.num_agents), lb=best_bws, ub=best_bws)
                for position in range(0, len(combo)):
                    # 0, 1, 2 -> 0, 2, 4
                    chunk_quality = combo[position]
                    index = last_index + position  # e.g., if last chunk is 3, then first iter is 3+0+1=4
                    download_time = 0
                    if best_ho_positions[agent_id] == position:
                        # Give them a penalty
                        download_time += HANDOVER_DELAY

                    download_time += (self.video_size[chunk_quality][index] / B_IN_MB) \
                                     / best_bws[agent_id][position] * BITS_IN_BYTE  # this is MB/MB/s --> seconds

                    if curr_buffer < download_time:
                        curr_rebuffer_time += (download_time - curr_buffer)
                        curr_buffer = 0.0
                    else:
                        curr_buffer -= download_time
                    curr_buffer += VIDEO_CHUNCK_LEN / MILLISECONDS_IN_SECOND

                    # bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                    # smoothness_diffs += abs(VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                    bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                    smoothness_diff += abs(
                        VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                    last_quality = chunk_quality
                # compute reward for this combination (one reward per 5-chunk combo)

                # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

                # 10~140 - 0~100 - 0~130
                rewards.append(bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * curr_rebuffer_time) \
                               - SMOOTH_PENALTY * smoothness_diff / M_IN_K)

            if np.nanmean(rewards) > np.nanmean(max_rewards):
                best_combos = combos
                max_rewards = rewards
                # ho_stamps = ho_positions
            elif np.nanmean(rewards) == np.nanmean(max_rewards) and np.nansum(combos[agent]) >= np.nansum(
                    best_combos[agent]):
                # elif np.nanmean(rewards) == np.nanmean(max_rewards) \
                #         and (rewards[agent] >= max_rewards[agent] or combos[agent][0] >= best_combos[agent][0]):
                best_combos = combos
                max_rewards = rewards
                # ho_stamps = ho_positions

        # return runner_up_sat_ids[agent], ho_stamps[agent], best_combos[agent], max_rewards[agent]

        return runner_up_sat_ids, best_ho_positions, best_combos, max_rewards

    def calculate_mpc_with_handover_exhaustive_ratio_reduced(self, agent):
        # future chunks length (try 4 if that many remaining)
        video_chunk_remain = [self.video_chunk_remain[i] for i in range(self.num_agents)]
        # last_index = self.get_total_video_chunk() - video_chunk_remain

        chunk_combo_option = []
        ho_combo_option = []
        # make chunk combination options
        for combo in itertools.product(list(range(int(BITRATE_LEVELS / BITRATE_WEIGHT))),
                                       repeat=MPC_FUTURE_CHUNK_COUNT * self.num_agents):
            chunk_combo_option.append(list([BITRATE_WEIGHT * x for x in combo]))

        # make handover combination options
        for combo in itertools.product(list(range(MPC_FUTURE_CHUNK_COUNT + 1)), repeat=self.num_agents):
            ho_combo_option.append(list(combo))

        future_chunk_length = [MPC_FUTURE_CHUNK_COUNT] * self.num_agents
        for i in range(self.num_agents):
            if video_chunk_remain[i] < MPC_FUTURE_CHUNK_COUNT:
                future_chunk_length[i] = video_chunk_remain[i]

        cur_download_bws = [self.predict_download_bw(i, True) for i in range(self.num_agents)]

        cur_sat_ids = [self.cur_sat_id[i] for i in range(self.num_agents)]
        runner_up_sat_ids = [self.get_runner_up_sat_id(i, method="harmonic-mean")[0] for i in range(self.num_agents)]

        related_sat_ids = list(set(cur_sat_ids + runner_up_sat_ids))
        num_of_sats = {}
        for sat_id in related_sat_ids:
            num_of_sats[sat_id] = self.get_num_of_user_sat(sat_id)

        start_buffers = [self.buffer_size[i] / MILLISECONDS_IN_SECOND for i in range(self.num_agents)]

        next_download_bws = []
        for agent_id in range(self.num_agents):
            for i in range(MPC_PAST_CHUNK_COUNT, 0, -1):
                self.predict_bw(runner_up_sat_ids[agent_id], agent_id, True,
                                mahimahi_ptr=self.mahimahi_ptr[agent_id] - i,
                                plus=False)
                self.predict_bw(cur_sat_ids[agent_id], agent_id, True, mahimahi_ptr=self.mahimahi_ptr[agent_id] - i,
                                plus=False)

            tmp_next_bw = self.predict_bw(runner_up_sat_ids[agent_id], agent_id, True)
            tmp_cur_bw = self.predict_bw(cur_sat_ids[agent_id], agent_id, True)
            if cur_download_bws[agent_id] is None:
                next_download_bws.append(None)
            else:
                assert cur_download_bws[agent_id] * tmp_next_bw / tmp_cur_bw != 0.0
                next_download_bws.append(cur_download_bws[agent_id] * tmp_next_bw / tmp_cur_bw)

        max_rewards = [-10000000 for _ in range(self.num_agents)]
        best_combos = [[self.last_quality[i]] for i in range(self.num_agents)]
        best_bws = [[-10000000] * MPC_FUTURE_CHUNK_COUNT for _ in range(self.num_agents)]
        best_bws_sum = [-10000000]
        best_ho_positions = {}

        sat_user_nums = num_of_sats

        best_user_info = None

        future_sat_user_nums = None
        future_sat_user_list = None

        for ho_positions in ho_combo_option:
            tmp_future_sat_user_nums = {}
            tmp_future_sat_user_list = {}
            tmp_bws = []
            tmp_bws_sum = []

            for sat_id in sat_user_nums.keys():
                tmp_future_sat_user_nums[sat_id] = np.array([sat_user_nums[sat_id]] * MPC_FUTURE_CHUNK_COUNT)
                tmp_future_sat_user_list[sat_id] = {}
                for i in range(MPC_FUTURE_CHUNK_COUNT):
                    tmp_future_sat_user_list[sat_id][i] = copy.deepcopy(self.cur_satellite[sat_id].get_ue_list())

            for idx, ho_point in enumerate(ho_positions):
                cur_sat_id = cur_sat_ids[idx]
                next_sat_id = runner_up_sat_ids[idx]
                cur_nums = tmp_future_sat_user_nums[cur_sat_id]
                next_nums = tmp_future_sat_user_nums[next_sat_id]

                cur_nums[ho_point:] = cur_nums[ho_point:] - 1
                next_nums[ho_point:] = next_nums[ho_point:] + 1

                for i in range(MPC_FUTURE_CHUNK_COUNT):
                    if i >= ho_point:
                        tmp_future_sat_user_list[cur_sat_id][i].remove(idx)
                        tmp_future_sat_user_list[next_sat_id][i].append(idx)

                tmp_future_sat_user_nums[cur_sat_id] = cur_nums
                tmp_future_sat_user_nums[next_sat_id] = next_nums

            for idx in range(self.num_agents):
                if cur_download_bws[idx] is None:
                    tmp_bws.append([np.nan])
                    tmp_bws_sum.append(np.nan)
                    continue

                cur_sat_id = cur_sat_ids[idx]
                next_sat_id = runner_up_sat_ids[idx]

                cur_sat_user_num = 1 if sat_user_nums[cur_sat_id] == 0 \
                    else sat_user_nums[cur_sat_id]
                next_sat_user_num = 1 if sat_user_nums[next_sat_id] == 0 \
                    else sat_user_nums[next_sat_id]
                bw_log = []
                for position in range(MPC_FUTURE_CHUNK_COUNT):
                    # cur_sat_user_num = sat_user_nums[cur_sat_id]
                    cur_future_sat_user_num = tmp_future_sat_user_nums[cur_sat_id][position]
                    # next_sat_user_num = sat_user_nums[next_sat_id]
                    next_future_sat_user_num = tmp_future_sat_user_nums[next_sat_id][position]

                    if ho_positions[idx] > position:
                        harmonic_bw = cur_download_bws[idx] * cur_sat_user_num / cur_future_sat_user_num
                    elif ho_positions[idx] == position:
                        harmonic_bw = next_download_bws[idx] * next_sat_user_num / next_future_sat_user_num
                    else:
                        harmonic_bw = next_download_bws[idx] * next_sat_user_num / next_future_sat_user_num

                    bw_log.append(harmonic_bw)
                    tmp_bws_sum.append(harmonic_bw)
                tmp_bws.append(bw_log)

            if np.nanmean(best_bws_sum) < np.nanmean(tmp_bws_sum):
                best_bws = tmp_bws
                best_ho_positions = ho_positions
                best_bws_sum = tmp_bws_sum
                future_sat_user_nums = tmp_future_sat_user_nums
                future_sat_user_list = tmp_future_sat_user_list
            elif np.nanmean(best_bws_sum) == np.nanmean(tmp_bws_sum) and best_ho_positions[agent] <= ho_positions[
                agent]:
                best_bws = tmp_bws
                best_ho_positions = ho_positions
                best_bws_sum = tmp_bws_sum
                future_sat_user_nums = tmp_future_sat_user_nums
                future_sat_user_list = tmp_future_sat_user_list

        for full_combo in chunk_combo_option:
            combos = []
            # Break at the end of the chunk

            for agent_id in range(self.num_agents):
                cur_combo = full_combo[
                            MPC_FUTURE_CHUNK_COUNT * agent_id:
                            MPC_FUTURE_CHUNK_COUNT * agent_id + future_chunk_length[agent_id]]
                # if cur_download_bws[agent_id] is None and cur_combo != [DEFAULT_QUALITY] * MPC_FUTURE_CHUNK_COUNT:
                #     wrong_format = True
                #     break
                if cur_download_bws[agent_id] is None:
                    combos.append([np.nan] * MPC_FUTURE_CHUNK_COUNT)
                else:
                    combos.append(cur_combo)

            bw_ratio = {}
            user_info = {}
            op_vars = []
            op_vars_index = 0
            bounds = []
            constraints = []
            sat_id_list = []
            const_array = []
            for sat_id in future_sat_user_nums.keys():
                user_list = []
                is_multi_users = False
                for i in range(len(future_sat_user_nums[sat_id])):
                    if future_sat_user_nums[sat_id][i] > 1:
                        is_multi_users = True
                        user_list = [*user_list, *future_sat_user_list[sat_id][i]]
                if is_multi_users:
                    user_list = list(set(user_list))
                    user_info[sat_id] = (op_vars_index, op_vars_index + len(user_list), user_list)

                    op_vars = [*op_vars, *([1 / len(user_list)] * len(user_list))]
                    bounds = [*bounds, *[(0 + EPSILON, 1 - EPSILON) for _ in range(len(user_list))]]
                    sat_id_list.append(sat_id)

                    target_array = np.zeros(op_vars_index + len(user_list))
                    target_array[op_vars_index:op_vars_index + len(user_list)] = 1

                    op_vars_index += len(user_list)

                    const_array.append(target_array)
            if op_vars:
                for i in range(len(const_array)):
                    data = const_array[i]
                    if len(const_array[i]) < op_vars_index:
                        data = np.append(const_array[i], [0] * (op_vars_index - len(const_array[i])))

                    constraint = LinearConstraint(data, lb=1, ub=1)

                    # constraints = [*constraints, {'type': 'eq', 'fun': const}]
                    constraints.append(constraint)
                import warnings
                warnings.filterwarnings("ignore")
                ue_ratio = minimize(
                    self.objective_function,
                    x0=np.array(op_vars),
                    args=(combos, cur_sat_ids, runner_up_sat_ids, sat_user_nums,
                          future_sat_user_nums, best_ho_positions, start_buffers,
                          video_chunk_remain, cur_download_bws,
                          next_download_bws, user_info, bw_ratio),
                    constraints=constraints,
                    bounds=bounds,
                    method="SLSQP"  # or BFGS
                )
                for sat_id in sat_id_list:
                    user_info[sat_id] = user_info[sat_id] + (ue_ratio.x[user_info[sat_id][0]:user_info[sat_id][1]],)
                    # user_info[sat_id] = user_info[sat_id] + (np.array([0.5, 0.5]),)

            rewards = []
            for agent_id, combo in enumerate(combos):
                if combo == [np.nan] * MPC_FUTURE_CHUNK_COUNT:
                    rewards.append(np.nan)
                    continue
                curr_rebuffer_time = 0
                curr_buffer = start_buffers[agent_id]
                bitrate_sum = 0
                smoothness_diff = 0
                last_quality = self.last_quality[agent_id]
                last_index = int(CHUNK_TIL_VIDEO_END_CAP - video_chunk_remain[agent_id])
                # linear optimization
                # constraint = LinearConstraint(np.ones(self.num_agents), lb=best_bws, ub=best_bws)

                cur_sat_id = cur_sat_ids[agent_id]
                next_sat_id = runner_up_sat_ids[agent_id]

                for position in range(0, len(combo)):
                    # 0, 1, 2 -> 0, 2, 4
                    chunk_quality = combo[position]
                    index = last_index + position  # e.g., if last chunk is 3, then first iter is 3+0+1=4
                    download_time = 0

                    # cur_sat_user_num = sat_user_nums[cur_sat_id]
                    cur_future_sat_user_num = future_sat_user_nums[cur_sat_id][position]
                    # next_sat_user_num = sat_user_nums[next_sat_id]
                    next_future_sat_user_num = future_sat_user_nums[next_sat_id][position]

                    now_sat_id = None
                    harmonic_bw = best_bws[agent_id][position]
                    if best_ho_positions[agent_id] > position:
                        if cur_future_sat_user_num > 1:
                            now_sat_id = cur_sat_id
                            harmonic_bw *= cur_future_sat_user_num
                    elif best_ho_positions[agent_id] == position:
                        if next_future_sat_user_num > 1:
                            now_sat_id = next_sat_id
                            harmonic_bw *= next_future_sat_user_num
                        # Give them a penalty
                        download_time += HANDOVER_DELAY
                    else:
                        if next_future_sat_user_num > 1:
                            now_sat_id = next_sat_id
                            harmonic_bw *= next_future_sat_user_num

                    if now_sat_id:
                        var_index = user_info[now_sat_id][2].index(agent_id)
                        harmonic_bw *= user_info[now_sat_id][3][var_index]

                    assert harmonic_bw != 0
                    download_time += (self.video_size[chunk_quality][index] / B_IN_MB) \
                                     / harmonic_bw * BITS_IN_BYTE  # this is MB/MB/s --> seconds

                    if curr_buffer < download_time:
                        curr_rebuffer_time += (download_time - curr_buffer)
                        curr_buffer = 0.0
                    else:
                        curr_buffer -= download_time
                    curr_buffer += VIDEO_CHUNCK_LEN / MILLISECONDS_IN_SECOND

                    # bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                    # smoothness_diffs += abs(VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                    bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                    smoothness_diff += abs(
                        VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                    last_quality = chunk_quality
                # compute reward for this combination (one reward per 5-chunk combo)

                # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

                # 10~140 - 0~100 - 0~130
                rewards.append(bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * curr_rebuffer_time) \
                               - SMOOTH_PENALTY * smoothness_diff / M_IN_K)

            if np.nanmean(rewards) > np.nanmean(max_rewards):
                best_combos = combos
                max_rewards = rewards
                # ho_stamps = ho_positions
                best_user_info = user_info

            elif np.nanmean(rewards) == np.nanmean(max_rewards) and np.nansum(combos[agent]) >= np.nansum(
                    best_combos[agent]):
                # elif np.nanmean(rewards) == np.nanmean(max_rewards) \
                #         and (rewards[agent] >= max_rewards[agent] or combos[agent][0] >= best_combos[agent][0]):
                best_combos = combos
                max_rewards = rewards
                # ho_stamps = ho_positions
                best_user_info = user_info

        # return runner_up_sat_ids[agent], ho_stamps[agent], best_combos[agent], max_rewards[agent]
        # print(future_sat_user_nums, cur_sat_ids, runner_up_sat_ids, best_ho_positions, best_combos, max_rewards, best_user_info)

        return runner_up_sat_ids, best_ho_positions, best_combos, max_rewards, best_user_info

    def calculate_mpc_with_handover(self, agent, robustness=True, only_runner_up=True,
                                    method="harmonic-mean", centralized=True):
        # future chunks length (try 4 if that many remaining)
        video_chunk_remain = self.video_chunk_remain[agent]
        # last_index = self.get_total_video_chunk() - video_chunk_remain
        last_index = int(CHUNK_TIL_VIDEO_END_CAP - video_chunk_remain)
        cooked_bw = self.cooked_bw[agent]

        chunk_combo_option = []
        # make chunk combination options
        for combo in itertools.product(list(range(int(BITRATE_LEVELS / BITRATE_WEIGHT))),
                                       repeat=MPC_FUTURE_CHUNK_COUNT):
            chunk_combo_option.append(list([BITRATE_WEIGHT * x for x in combo]))

        future_chunk_length = MPC_FUTURE_CHUNK_COUNT
        if video_chunk_remain < MPC_FUTURE_CHUNK_COUNT:
            future_chunk_length = video_chunk_remain

        max_reward = -10000000
        best_combo = (self.last_quality[agent],)
        ho_sat_id = self.cur_sat_id[agent]
        ho_stamp = MPC_FUTURE_CHUNK_COUNT
        if future_chunk_length == 0:
            return ho_sat_id, ho_stamp, best_combo, max_reward

        cur_user_num = self.get_num_of_user_sat(self.cur_sat_id[agent])
        cur_download_bw, runner_up_sat_id = None, None
        if method == "harmonic-mean":
            cur_download_bw = self.predict_download_bw(agent, True)
            runner_up_sat_id, _ = self.get_runner_up_sat_id(
                agent, method="harmonic-mean")
        elif method == "holt-winter":
            cur_download_bw = self.predict_download_bw_holt_winter(agent)
            # cur_download_bw = self.predict_download_bw(agent, True)
            runner_up_sat_id, _ = self.get_runner_up_sat_id(
                agent, method="holt-winter")
        else:
            print("Cannot happen")
            exit(1)

        start_buffer = self.buffer_size[agent] / MILLISECONDS_IN_SECOND

        best_combo, max_reward, best_case = self.calculate_mpc(video_chunk_remain, start_buffer, last_index,
                                                               cur_download_bw, agent, centralized)

        for next_sat_id, next_sat_bw in cooked_bw.items():

            if next_sat_id == self.cur_sat_id[agent]:
                continue

            elif only_runner_up and runner_up_sat_id != next_sat_id:
                # Only consider the next-best satellite
                continue
            else:
                # Check if it is visible now
                if cooked_bw[next_sat_id][self.mahimahi_ptr[agent] - 1] != 0.0 and cooked_bw[next_sat_id][
                    self.mahimahi_ptr[agent]] != 0.0:
                    # Pass the previously connected satellite
                    # if next_sat_id == self.prev_sat_id[agent]:
                    #     continue
                    # Based on the bw, not download bw
                    next_download_bw = None
                    if method == "harmonic-mean":
                        for i in range(MPC_PAST_CHUNK_COUNT, 0, -1):
                            self.predict_bw(next_sat_id, agent, robustness, mahimahi_ptr=self.mahimahi_ptr[agent] - i,
                                            plus=True)
                            self.predict_bw(self.cur_sat_id[agent], agent, robustness,
                                            mahimahi_ptr=self.mahimahi_ptr[agent] - i, plus=False)

                        tmp_next_bw = self.predict_bw(next_sat_id, agent, robustness, plus=True)
                        tmp_cur_bw = self.predict_bw(self.cur_sat_id[agent], agent, robustness)
                        next_download_bw = cur_download_bw * tmp_next_bw / tmp_cur_bw

                    elif method == "holt-winter":
                        # next_harmonic_bw = self.predict_bw_holt_winter(next_sat_id, mahimahi_ptr, num=1)
                        # Change to proper download bw
                        next_download_bw = cur_download_bw * cooked_bw[next_sat_id][self.mahimahi_ptr[agent] - 1] / \
                                           (cooked_bw[self.cur_sat_id[agent]][
                                                self.mahimahi_ptr[agent] - 1] / cur_user_num)

                    else:
                        print("Cannot happen")
                        exit(1)

                    for ho_index in range(MPC_FUTURE_CHUNK_COUNT):
                        # all possible combinations of 5 chunk bitrates for 6 bitrate options (6^5 options)
                        # iterate over list and for each, compute reward and store max reward combination
                        # ho_index: 0-4 -> Do handover, 5 -> Do not handover
                        for full_combo in chunk_combo_option:
                            # Break at the end of the chunk
                            combo = full_combo[0: future_chunk_length]
                            # calculate total rebuffer time for this combination (start with start_buffer and subtract
                            # each download time and add 2 seconds in that order)
                            curr_rebuffer_time = 0
                            curr_buffer = start_buffer
                            bitrate_sum = 0
                            smoothness_diffs = 0
                            last_quality = self.last_quality[agent]

                            for position in range(0, len(combo)):
                                chunk_quality = combo[position]
                                index = last_index + position  # e.g., if last chunk is 3, then first iter is 3+0+1=4
                                download_time = 0
                                if ho_index > position:
                                    harmonic_bw = cur_download_bw
                                elif ho_index == position:
                                    harmonic_bw = next_download_bw
                                    # Give them a penalty
                                    download_time += HANDOVER_DELAY
                                else:
                                    harmonic_bw = next_download_bw

                                download_time += (self.video_size[chunk_quality][index] / B_IN_MB) \
                                                 / harmonic_bw * BITS_IN_BYTE  # this is MB/MB/s --> seconds

                                if curr_buffer < download_time:
                                    curr_rebuffer_time += (download_time -
                                                           curr_buffer)
                                    curr_buffer = 0.0
                                else:
                                    curr_buffer -= download_time
                                curr_buffer += VIDEO_CHUNCK_LEN / MILLISECONDS_IN_SECOND

                                # bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                                # smoothness_diffs += abs(VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                                bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                                smoothness_diffs += abs(
                                    VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                                last_quality = chunk_quality
                            # compute reward for this combination (one reward per 5-chunk combo)

                            # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

                            # 10~140 - 0~100 - 0~130
                            reward = bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * curr_rebuffer_time) \
                                     - SMOOTH_PENALTY * smoothness_diffs / M_IN_K

                            if centralized:
                                for agent_id in range(self.num_agents):
                                    if agent_id == agent or self.user_qoe_log[agent_id] == {}:
                                        continue
                                    qoe_log = self.user_qoe_log[agent_id]
                                    reward += self.get_simulated_reward(qoe_log, last_index, ho_index,
                                                                        self.cur_sat_id[agent], next_sat_id)
                                    # reward += qoe_log["reward"]

                            next_user_num = self.get_num_of_user_sat(next_sat_id)

                            if reward > max_reward:
                                best_combo = combo
                                max_reward = reward
                                ho_sat_id = next_sat_id
                                ho_stamp = ho_index
                                best_case = {"last_quality": last_quality, "cur_download_bw": cur_download_bw,
                                             "start_buffer": start_buffer, "future_chunk_length": future_chunk_length,
                                             "last_index": last_index, "combo": combo,
                                             "next_download_bw": next_download_bw,
                                             "ho_index": ho_index, "next_sat_id": next_sat_id, "reward": reward,
                                             "cur_user_num": cur_user_num, "next_user_num": next_user_num,
                                             "cur_sat_id": self.cur_sat_id[agent]}
                            elif reward == max_reward and (combo[0] >= best_combo[0]):
                                best_combo = combo
                                max_reward = reward
                                ho_sat_id = next_sat_id
                                ho_stamp = ho_index
                                best_case = {"last_quality": last_quality, "cur_download_bw": cur_download_bw,
                                             "start_buffer": start_buffer, "future_chunk_length": future_chunk_length,
                                             "last_index": last_index, "combo": combo,
                                             "next_download_bw": next_download_bw,
                                             "ho_index": ho_index, "next_sat_id": next_sat_id, "reward": reward,
                                             "cur_user_num": cur_user_num, "next_user_num": next_user_num,
                                             "cur_sat_id": self.cur_sat_id[agent]}

        self.user_qoe_log[agent] = best_case
        return ho_sat_id, ho_stamp, best_combo, max_reward

    def objective_function(self, x, combos, cur_sat_ids, runner_up_sat_ids, sat_user_nums,
                           future_sat_user_nums, ho_positions, start_buffers, video_chunk_remain,
                           cur_download_bws, next_download_bws, user_info, bw_ratio):

        rewards = []
        curr_rebuffer_time = 0
        for agent_id, combo in enumerate(combos):
            if combo == [np.nan] * MPC_FUTURE_CHUNK_COUNT:
                rewards.append(np.nan)
                continue

            curr_buffer = start_buffers[agent_id]
            last_index = int(CHUNK_TIL_VIDEO_END_CAP - video_chunk_remain[agent_id])
            cur_sat_id = cur_sat_ids[agent_id]
            next_sat_id = runner_up_sat_ids[agent_id]

            cur_sat_user_num = 1 if sat_user_nums[cur_sat_id] == 0 \
                else sat_user_nums[cur_sat_id]
            next_sat_user_num = 1 if sat_user_nums[next_sat_id] == 0 \
                else sat_user_nums[next_sat_id]

            for position in range(0, len(combo)):
                # 0, 1, 2 -> 0, 2, 4
                chunk_quality = combo[position]
                index = last_index + position  # e.g., if last chunk is 3, then first iter is 3+0+1=4
                download_time = 0

                # cur_sat_user_num = sat_user_nums[cur_sat_id]
                cur_future_sat_user_num = future_sat_user_nums[cur_sat_id][position]
                # next_sat_user_num = sat_user_nums[next_sat_id]
                next_future_sat_user_num = future_sat_user_nums[next_sat_id][position]

                if ho_positions[agent_id] > position:
                    now_future_sat_user_num = cur_future_sat_user_num
                    now_download_bws = cur_download_bws[agent_id] * cur_sat_user_num
                    now_sat_id = cur_sat_id
                elif ho_positions[agent_id] == position:
                    now_future_sat_user_num = next_future_sat_user_num
                    now_download_bws = next_download_bws[agent_id] * next_sat_user_num
                    now_sat_id = next_sat_id
                    # Give them a penalty
                    download_time += HANDOVER_DELAY
                else:
                    now_future_sat_user_num = next_future_sat_user_num
                    now_download_bws = next_download_bws[agent_id] * next_sat_user_num
                    now_sat_id = next_sat_id

                assert now_download_bws != 0

                if now_future_sat_user_num > 1:
                    assert now_sat_id in user_info.keys()
                    var_index = user_info[now_sat_id][2].index(agent_id)
                    download_time += (self.video_size[chunk_quality][index] / B_IN_MB) \
                                     / (now_download_bws * x[var_index]) * BITS_IN_BYTE  # this is MB/MB/s --> seconds
                else:
                    download_time += (self.video_size[chunk_quality][index] / B_IN_MB) \
                                     / now_download_bws * BITS_IN_BYTE  # this is MB/MB/s --> seconds

                if curr_buffer < download_time:
                    curr_rebuffer_time += (download_time - curr_buffer)
                    curr_buffer = 0.0
                else:
                    curr_buffer -= download_time
                curr_buffer += VIDEO_CHUNCK_LEN / MILLISECONDS_IN_SECOND

            # compute reward for this combination (one reward per 5-chunk combo)

            # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

            # 10~140 - 0~100 - 0~130
        return curr_rebuffer_time

    """
    def calculate_cent_mpc(self, robustness=True, only_runner_up=True,
                                    method="harmonic-mean", centralized=True):
        # future chunks length (try 4 if that many remaining)
        video_chunk_remain = self.video_chunk_remain[agent]
        # last_index = self.get_total_video_chunk() - video_chunk_remain
        last_index = int(CHUNK_TIL_VIDEO_END_CAP - video_chunk_remain)

        chunk_combo_option = []
        # make chunk combination options
        for combo in itertools.product(list(range(BITRATE_LEVELS)), repeat=MPC_FUTURE_CHUNK_COUNT):
            chunk_combo_option.append(combo)

        future_chunk_length = MPC_FUTURE_CHUNK_COUNT
        if video_chunk_remain < MPC_FUTURE_CHUNK_COUNT:
            future_chunk_length = video_chunk_remain

        max_reward = -10000000
        best_combo = set(self.last_quality)
        ho_sat_id = self.cur_sat_id
        ho_stamp = MPC_FUTURE_CHUNK_COUNT
        if future_chunk_length == 0:
            return ho_sat_id, ho_stamp, best_combo, max_reward

        cur_user_num = self.get_num_of_user_sat(self.cur_sat_id[agent])
        cur_download_bw, runner_up_sat_id = None, None
        if method == "harmonic-mean":
            cur_download_bw = self.predict_download_bw(agent, True)
            runner_up_sat_id, _ = self.get_runner_up_sat_id(
                agent, method="harmonic-mean")
        elif method == "holt-winter":
            cur_download_bw = self.predict_download_bw_holt_winter(agent)
            # cur_download_bw = self.predict_download_bw(agent, True)
            runner_up_sat_id, _ = self.get_runner_up_sat_id(
                agent, method="holt-winter")
        else:
            print("Cannot happen")
            exit(1)

        start_buffer = np.array(self.buffer_size) / MILLISECONDS_IN_SECOND

        best_combo, max_reward, best_case = self.calculate_mpc(video_chunk_remain, start_buffer, last_index,
                                                               cur_download_bw, agent, centralized)

        for next_sat_id, next_sat_bw in self.cooked_bw.items():

            if next_sat_id == self.cur_sat_id[agent]:
                continue
            else:
                # Check if it is visible now
                if self.cooked_bw[next_sat_id][self.mahimahi_ptr[agent] - 1] != 0.0 and self.cooked_bw[next_sat_id][
                    self.mahimahi_ptr[agent]] != 0.0:
                    # Pass the previously connected satellite
                    # if next_sat_id == self.prev_sat_id[agent]:
                    #     continue

                    if only_runner_up and runner_up_sat_id != next_sat_id:
                        # Only consider the next-best satellite
                        continue
                    # Based on the bw, not download bw
                    next_download_bw = None
                    if method == "harmonic-mean":
                        for i in range(MPC_FUTURE_CHUNK_COUNT, 0, -1):
                            self.predict_bw(next_sat_id, agent, robustness, mahimahi_ptr=self.mahimahi_ptr[agent] - i,
                                            plus=True)
                            self.predict_bw(self.cur_sat_id[agent], agent, robustness,
                                            mahimahi_ptr=self.mahimahi_ptr[agent] - i, plus=False)

                        tmp_next_bw = self.predict_bw(next_sat_id, agent, robustness)
                        tmp_cur_bw = self.predict_bw(self.cur_sat_id[agent], agent, robustness)
                        next_download_bw = cur_download_bw * tmp_next_bw / tmp_cur_bw

                    elif method == "holt-winter":
                        # next_harmonic_bw = self.predict_bw_holt_winter(next_sat_id, mahimahi_ptr, num=1)
                        # Change to proper download bw
                        next_download_bw = cur_download_bw * self.cooked_bw[next_sat_id][self.mahimahi_ptr[agent] - 1] / \
                                           (self.cooked_bw[self.cur_sat_id[agent]][
                                                self.mahimahi_ptr[agent] - 1] / cur_user_num)
                    else:
                        print("Cannot happen")
                        exit(1)

                    for ho_index in range(MPC_FUTURE_CHUNK_COUNT):
                        # all possible combinations of 5 chunk bitrates for 6 bitrate options (6^5 options)
                        # iterate over list and for each, compute reward and store max reward combination
                        # ho_index: 0-4 -> Do handover, 5 -> Do not handover
                        for full_combo in chunk_combo_option:
                            # Break at the end of the chunk
                            combo = full_combo[0: future_chunk_length]
                            # calculate total rebuffer time for this combination (start with start_buffer and subtract
                            # each download time and add 2 seconds in that order)
                            curr_rebuffer_time = 0
                            curr_buffer = start_buffer
                            bitrate_sum = 0
                            smoothness_diffs = 0
                            last_quality = self.last_quality[agent]

                            for position in range(0, len(combo)):
                                chunk_quality = combo[position]
                                index = last_index + position  # e.g., if last chunk is 3, then first iter is 3+0+1=4
                                download_time = 0
                                if ho_index > position:
                                    harmonic_bw = cur_download_bw
                                elif ho_index == position:
                                    harmonic_bw = next_download_bw
                                    # Give them a penalty
                                    download_time += HANDOVER_DELAY
                                else:
                                    harmonic_bw = next_download_bw
                                download_time += (self.video_size[chunk_quality][index] / B_IN_MB) \
                                                 / harmonic_bw * BITS_IN_BYTE  # this is MB/MB/s --> seconds

                                if curr_buffer < download_time:
                                    curr_rebuffer_time += (download_time -
                                                           curr_buffer)
                                    curr_buffer = 0.0
                                else:
                                    curr_buffer -= download_time
                                curr_buffer += VIDEO_CHUNCK_LEN / MILLISECONDS_IN_SECOND

                                # bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                                # smoothness_diffs += abs(VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                                bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                                smoothness_diffs += abs(
                                    VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                                last_quality = chunk_quality
                            # compute reward for this combination (one reward per 5-chunk combo)

                            # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

                            # 10~140 - 0~100 - 0~130
                            reward = bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * curr_rebuffer_time) \
                                     - SMOOTH_PENALTY * smoothness_diffs / M_IN_K

                            if centralized:
                                for agent_id in range(self.num_agents):
                                    if agent_id == agent:
                                        continue
                                    qoe_log = self.user_qoe_log[agent_id]
                                    reward += self.get_simulated_reward(qoe_log, last_index, ho_index,
                                                                        self.cur_sat_id[agent], next_sat_id)
                                    # reward += qoe_log["reward"]

                            next_user_num = self.get_num_of_user_sat(next_sat_id)

                            if reward > max_reward:
                                best_combo = combo
                                max_reward = reward
                                ho_sat_id = next_sat_id
                                ho_stamp = ho_index
                                best_case = {"last_quality": last_quality, "cur_download_bw": cur_download_bw,
                                             "start_buffer": start_buffer, "future_chunk_length": future_chunk_length,
                                             "last_index": last_index, "combo": combo,
                                             "next_download_bw": next_download_bw,
                                             "ho_index": ho_index, "next_sat_id": next_sat_id, "reward": reward,
                                             "cur_user_num": cur_user_num, "next_user_num": next_user_num,
                                             "cur_sat_id": self.cur_sat_id[agent]}
                            elif reward == max_reward and (combo[0] >= best_combo[0] or ho_index >= 0):
                                best_combo = combo
                                max_reward = reward
                                ho_sat_id = next_sat_id
                                ho_stamp = ho_index
                                best_case = {"last_quality": last_quality, "cur_download_bw": cur_download_bw,
                                             "start_buffer": start_buffer, "future_chunk_length": future_chunk_length,
                                             "last_index": last_index, "combo": combo,
                                             "next_download_bw": next_download_bw,
                                             "ho_index": ho_index, "next_sat_id": next_sat_id, "reward": reward,
                                             "cur_user_num": cur_user_num, "next_user_num": next_user_num,
                                             "cur_sat_id": self.cur_sat_id[agent]}

        self.user_qoe_log[agent] = best_case
        return ho_sat_id, ho_stamp, best_combo, max_reward
    """

    def predict_download_bw_holt_winter(self, agent, m=172):
        cur_sat_past_list = self.download_bw[agent]
        if len(cur_sat_past_list) <= 1:
            return self.download_bw[agent][-1]
        past_bws = cur_sat_past_list[-MPC_PAST_CHUNK_COUNT:]
        # past_bws = cur_sat_past_list
        # print(past_bws)
        while past_bws[0] == 0.0:
            past_bws = past_bws[1:]

        cur_sat_past_bws = pd.Series(past_bws)
        cur_sat_past_bws.index.freq = 's'

        # alpha = 1 / (2 * m)
        fitted_model = ExponentialSmoothing(
            cur_sat_past_bws, trend='add').fit()
        # fitted_model = ExponentialSmoothing(cur_sat_past_bws, trend='mul').fit()

        # fitted_model = ExponentialSmoothing(cur_sat_past_bws
        # test_predictions = fitted_model.forecast(3)
        test_predictions = fitted_model.forecast(1)

        pred_bw = sum(test_predictions) / len(test_predictions)

        return pred_bw

    def get_runner_up_sat_id(self, agent, method="holt-winter", mahimahi_ptr=None):
        best_sat_id = None
        best_sat_bw = 0
        cooked_bw = self.cooked_bw[agent]
        if mahimahi_ptr is None:
            mahimahi_ptr = self.mahimahi_ptr[agent]

        for sat_id, sat_bw in cooked_bw.items():
            real_sat_bw = None

            # Pass the previously connected satellite
            if sat_id == self.cur_sat_id[agent]:
                continue

            if method == "harmonic-mean":
                # target_sat_bw = self.predict_bw(sat_id, agent, mahimahi_ptr=mahimahi_ptr)
                real_sat_bw = sat_bw[mahimahi_ptr] / (self.get_num_of_user_sat(sat_id) + 1)
            elif method == "holt-winter":
                target_sat_bw = self.predict_bw_holt_winter(sat_id, agent, num=1)
                # target_sat_bw = sum(target_sat_bw) / len(target_sat_bw)
            else:
                print("Cannot happen")
                exit(1)

            assert (real_sat_bw is not None)
            if best_sat_bw < real_sat_bw:
                best_sat_id = sat_id
                best_sat_bw = real_sat_bw

        return best_sat_id, best_sat_bw

    def predict_bw_holt_winter(self, sat_id, agent, num=1):
        start_index = self.mahimahi_ptr[agent] - MPC_PAST_CHUNK_COUNT
        cur_sat_past_list = []
        cooked_bw = self.cooked_bw[agent]
        if start_index < 0:
            for i in range(0, start_index + MPC_PAST_CHUNK_COUNT):
                cur_sat_past_list.append(
                    cooked_bw[sat_id][0:start_index + MPC_PAST_CHUNK_COUNT] / self.get_num_of_user_sat(sat_id))
        else:
            for i in range(start_index, start_index + MPC_PAST_CHUNK_COUNT):
                cur_sat_past_list.append(
                    cooked_bw[sat_id][0:start_index + MPC_PAST_CHUNK_COUNT] / self.get_num_of_user_sat(sat_id))

        while len(cur_sat_past_list) != 0 and cur_sat_past_list[0] == 0.0:
            cur_sat_past_list = cur_sat_past_list[1:]

        if len(cur_sat_past_list) <= 1:
            # Just past bw
            return cooked_bw[sat_id][self.mahimahi_ptr[agent] - 1] / self.get_num_of_user_sat(sat_id)
        cur_sat_past_bws = pd.Series(cur_sat_past_list)
        cur_sat_past_bws.index.freq = 's'

        # alpha = 1 / (2 * m)
        fitted_model = ExponentialSmoothing(
            cur_sat_past_bws, trend='add').fit()
        # fitted_model = ExponentialSmoothing(cur_sat_past_bws, trend='mul').fit()

        # fitted_model = ExponentialSmoothing(cur_sat_past_bws
        test_predictions = fitted_model.forecast(1)
        # test_predictions = fitted_model.forecast(num)

        pred_bw = sum(test_predictions) / len(test_predictions)

        return pred_bw
        # return list(test_predictions)

    def calculate_mpc(self, video_chunk_remain, start_buffer, last_index, cur_download_bw, agent, centralized=False):
        max_reward = -10000000
        best_combo = ()
        chunk_combo_option = []
        best_case = {}

        # make chunk combination options
        for combo in itertools.product(list(range(int(BITRATE_LEVELS / BITRATE_WEIGHT))),
                                       repeat=MPC_FUTURE_CHUNK_COUNT):
            chunk_combo_option.append(list([BITRATE_WEIGHT * x for x in combo]))
        cur_user_num = self.get_num_of_user_sat(self.cur_sat_id[agent])
        future_chunk_length = MPC_FUTURE_CHUNK_COUNT
        if video_chunk_remain < MPC_FUTURE_CHUNK_COUNT:
            future_chunk_length = video_chunk_remain

        for full_combo in chunk_combo_option:
            # Break at the end of the chunk
            combo = full_combo[0: future_chunk_length]
            # calculate total rebuffer time for this combination (start with start_buffer and subtract
            # each download time and add 2 seconds in that order)
            curr_rebuffer_time = 0
            curr_buffer = start_buffer
            bitrate_sum = 0
            smoothness_diffs = 0
            last_quality = self.last_quality[agent]

            for position in range(0, len(combo)):
                chunk_quality = combo[position]
                index = last_index + position  # e.g., if last chunk is 3, then first iter is 3+0+1=4
                download_time = 0
                download_time += (self.video_size[chunk_quality][index] / B_IN_MB) \
                                 / cur_download_bw * BITS_IN_BYTE  # this is MB/MB/s --> seconds

                if curr_buffer < download_time:
                    curr_rebuffer_time += (download_time - curr_buffer)
                    curr_buffer = 0.0
                else:
                    curr_buffer -= download_time
                curr_buffer += VIDEO_CHUNCK_LEN / MILLISECONDS_IN_SECOND

                # bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                # smoothness_diffs += abs(VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
                smoothness_diffs += abs(
                    VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
                last_quality = chunk_quality
            # compute reward for this combination (one reward per 5-chunk combo)

            # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

            # 10~140 - 0~100 - 0~130
            reward = bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * curr_rebuffer_time) \
                     - SMOOTH_PENALTY * smoothness_diffs / M_IN_K

            if reward > max_reward:
                best_combo = combo
                max_reward = reward
                best_case = {"last_quality": last_quality, "cur_download_bw": cur_download_bw,
                             "start_buffer": start_buffer, "future_chunk_length": future_chunk_length,
                             "last_index": last_index, "combo": combo, "next_download_bw": None,
                             "ho_index": MPC_FUTURE_CHUNK_COUNT, "next_sat_id": None, "reward": reward,
                             "cur_user_num": cur_user_num, "cur_sat_id": self.cur_sat_id[agent], "next_user_num": 0}
            elif reward == max_reward and (combo[0] >= best_combo[0]):
                best_combo = combo
                max_reward = reward
                best_case = {"last_quality": last_quality, "cur_download_bw": cur_download_bw,
                             "start_buffer": start_buffer, "future_chunk_length": future_chunk_length,
                             "last_index": last_index, "combo": combo, "next_download_bw": None,
                             "ho_index": MPC_FUTURE_CHUNK_COUNT, "next_sat_id": None, "reward": reward,
                             "cur_user_num": cur_user_num, "cur_sat_id": self.cur_sat_id[agent], "next_user_num": 0}

        return best_combo, max_reward, best_case

    def predict_download_bw(self, agent, robustness=False):

        curr_error = 0

        if not self.download_bw[agent]:
            return None
        past_download_bw = self.download_bw[agent][-1]

        if len(self.past_download_ests[agent]) > 0:
            curr_error = abs(self.past_download_ests[agent][-1] - past_download_bw) / float(past_download_bw)
        self.past_download_bw_errors[agent].append(curr_error)

        # pick bitrate according to MPC
        # first get harmonic mean of last 5 bandwidths
        # past_bws = self.cooked_bw[self.cur_sat_id][start_index: self.mahimahi_ptr]
        past_bws = self.download_bw[agent][-MPC_PAST_CHUNK_COUNT:]
        while past_bws[0] == 0.0:
            past_bws = past_bws[1:]

        bandwidth_sum = 0
        for past_val in past_bws:
            bandwidth_sum += (1 / float(past_val))

        harmonic_bw = 1.0 / (bandwidth_sum / len(past_bws))
        self.past_download_ests[agent].append(harmonic_bw)

        if robustness:
            # future bandwidth prediction
            # divide by 1 + max of last 5 (or up to 5) errors
            error_pos = -MPC_PAST_CHUNK_COUNT
            if len(self.past_download_bw_errors[agent]) < MPC_PAST_CHUNK_COUNT:
                error_pos = -len(self.past_download_bw_errors[agent])
            max_error = float(max(self.past_download_bw_errors[agent][error_pos:]))
            harmonic_bw = harmonic_bw / (1 + max_error)  # robustMPC here

        return harmonic_bw

    def predict_bw(self, sat_id, agent, robustness=True, plus=False, mahimahi_ptr=None):
        curr_error = 0
        cooked_bw = self.cooked_bw[agent]
        if mahimahi_ptr is None:
            mahimahi_ptr = self.mahimahi_ptr[agent]

        if plus:
            num_of_user_sat = self.get_num_of_user_sat(sat_id) + 1
        else:
            num_of_user_sat = self.get_num_of_user_sat(sat_id)

        # past_bw = self.cooked_bw[self.cur_sat_id][self.mahimahi_ptr - 1]
        if num_of_user_sat == 0:
            past_bw = cooked_bw[sat_id][mahimahi_ptr - 1]
        else:
            past_bw = cooked_bw[sat_id][mahimahi_ptr - 1] / num_of_user_sat
        if past_bw == 0:
            return cooked_bw[sat_id][mahimahi_ptr]

        if sat_id in self.past_bw_ests[agent].keys() and len(self.past_bw_ests[agent][sat_id]) > 0 \
                and mahimahi_ptr - 1 in self.past_bw_ests[agent][sat_id].keys():
            curr_error = abs(self.past_bw_ests[agent][sat_id][mahimahi_ptr - 1] - past_bw) / float(past_bw)
        if sat_id not in self.past_bw_errors[agent].keys():
            self.past_bw_errors[agent][sat_id] = []
        self.past_bw_errors[agent][sat_id].append(curr_error)

        # pick bitrate according to MPC
        # first get harmonic mean of last 5 bandwidths
        start_index = mahimahi_ptr - MPC_PAST_CHUNK_COUNT
        if start_index < 0:
            start_index = 0

        past_bws = []
        for index in range(start_index, mahimahi_ptr):
            if num_of_user_sat == 0:
                past_bws.append(cooked_bw[sat_id][index])
            else:
                past_bws.append(cooked_bw[sat_id][index] / num_of_user_sat)

        # Newly possible satellite case
        if all(v == 0.0 for v in past_bws):
            if num_of_user_sat == 0:
                return cooked_bw[sat_id][mahimahi_ptr]
            else:
                return cooked_bw[sat_id][mahimahi_ptr] / num_of_user_sat

        while past_bws[0] == 0.0:
            past_bws = past_bws[1:]

        bandwidth_sum = 0
        for past_val in past_bws:
            bandwidth_sum += (1 / float(past_val))

        harmonic_bw = 1.0 / (bandwidth_sum / len(past_bws))

        if sat_id not in self.past_bw_ests[agent].keys():
            self.past_bw_ests[agent][sat_id] = {}
        if self.mahimahi_ptr[agent] not in self.past_bw_ests[agent][sat_id].keys():
            self.past_bw_ests[agent][sat_id][mahimahi_ptr] = None
        self.past_bw_ests[agent][sat_id][mahimahi_ptr] = harmonic_bw

        if robustness:
            # future bandwidth prediction
            # divide by 1 + max of last 5 (or up to 5) errors
            error_pos = -MPC_PAST_CHUNK_COUNT
            if sat_id in self.past_bw_errors[agent].keys() and len(
                    self.past_bw_errors[agent][sat_id]) < MPC_PAST_CHUNK_COUNT:
                error_pos = -len(self.past_bw_errors[agent][sat_id])
            max_error = float(max(self.past_bw_errors[agent][sat_id][error_pos:]))
            harmonic_bw = harmonic_bw / (1 + max_error)  # robustMPC here

        return harmonic_bw

    def predict_bw_set(self, sat_id, agent, future_len=1, robustness=True, plus=False, mahimahi_ptr=None):
        curr_error = 0
        cooked_bw = self.cooked_bw[agent]
        if mahimahi_ptr is None:
            mahimahi_ptr = self.mahimahi_ptr[agent]

        if plus:
            num_of_user_sat = self.get_num_of_user_sat(sat_id) + 1
        else:
            num_of_user_sat = self.get_num_of_user_sat(sat_id)

        # past_bw = self.cooked_bw[self.cur_sat_id][self.mahimahi_ptr - 1]
        if num_of_user_sat == 0:
            past_bw = cooked_bw[sat_id][mahimahi_ptr - 1]
        else:
            past_bw = cooked_bw[sat_id][mahimahi_ptr - 1] / num_of_user_sat
        if past_bw == 0:
            return 0

        if sat_id in self.past_bw_ests[agent].keys() and len(self.past_bw_ests[agent][sat_id]) > 0 \
                and mahimahi_ptr - 1 in self.past_bw_ests[agent][sat_id].keys():
            curr_error = abs(self.past_bw_ests[agent][sat_id][mahimahi_ptr - 1] - past_bw) / float(past_bw)
        if sat_id not in self.past_bw_errors[agent].keys():
            self.past_bw_errors[agent][sat_id] = []
        self.past_bw_errors[agent][sat_id].append(curr_error)

        # pick bitrate according to MPC
        # first get harmonic mean of last 5 bandwidths
        start_index = mahimahi_ptr - MPC_PAST_CHUNK_COUNT
        if start_index < 0:
            start_index = 0

        past_bws = []
        for index in range(start_index, mahimahi_ptr):
            if num_of_user_sat == 0:
                past_bws.append(cooked_bw[sat_id][index])
            else:
                past_bws.append(cooked_bw[sat_id][index] / num_of_user_sat)

        # Newly possible satellite case
        if all(v == 0.0 for v in past_bws):
            if num_of_user_sat == 0:
                return cooked_bw[sat_id][mahimahi_ptr]
            else:
                return cooked_bw[sat_id][mahimahi_ptr] / num_of_user_sat

        while past_bws[0] == 0.0:
            past_bws = past_bws[1:]

        bandwidth_sum = 0
        for past_val in past_bws:
            bandwidth_sum += (1 / float(past_val))

        harmonic_bw = 1.0 / (bandwidth_sum / len(past_bws))

        if sat_id not in self.past_bw_ests[agent].keys():
            self.past_bw_ests[agent][sat_id] = {}
        if self.mahimahi_ptr[agent] not in self.past_bw_ests[agent][sat_id].keys():
            self.past_bw_ests[agent][sat_id][mahimahi_ptr] = None
        self.past_bw_ests[agent][sat_id][mahimahi_ptr] = harmonic_bw

        if robustness:
            # future bandwidth prediction
            # divide by 1 + max of last 5 (or up to 5) errors
            error_pos = -MPC_PAST_CHUNK_COUNT
            if sat_id in self.past_bw_errors[agent].keys() and len(
                    self.past_bw_errors[agent][sat_id]) < MPC_PAST_CHUNK_COUNT:
                error_pos = -len(self.past_bw_errors[agent][sat_id])
            max_error = float(max(self.past_bw_errors[agent][sat_id][error_pos:]))
            harmonic_bw = harmonic_bw / (1 + max_error)  # robustMPC here

        return harmonic_bw

    def get_simulated_reward(self, qoe_log, target_last_index, target_ho_index, target_cur_sat_id, target_next_sat_id):
        combo = qoe_log["combo"]
        # calculate total rebuffer time for this combination (start with start_buffer and subtract
        # each download time and add 2 seconds in that order)
        curr_rebuffer_time = 0
        curr_buffer = qoe_log["start_buffer"]
        bitrate_sum = 0
        smoothness_diffs = 0
        last_quality = qoe_log["last_quality"]
        cur_user_num = qoe_log["cur_user_num"]
        ho_index = qoe_log["ho_index"]
        harmonic_bw = None
        for position in range(0, len(combo)):
            chunk_quality = combo[position]
            index = qoe_log["last_index"] + position  # e.g., if last chunk is 3, then first iter is 3+0+1=4
            download_time = 0
            if ho_index > position:
                if target_cur_sat_id == qoe_log["cur_sat_id"] and index >= target_last_index + target_ho_index:
                    # if self.get_num_of_user_sat(target_cur_sat_id) - 1 == 0:
                    if cur_user_num <= 1:
                        harmonic_bw = qoe_log["cur_download_bw"]
                    else:
                        harmonic_bw = qoe_log["cur_download_bw"] * (cur_user_num / (cur_user_num - 1))
                        # harmonic_bw = qoe_log["cur_download_bw"] * (cur_user_num / (self.get_num_of_user_sat(target_cur_sat_id) - 1))
                elif target_next_sat_id == qoe_log["cur_sat_id"] and index >= target_last_index + target_ho_index:
                    if cur_user_num < 1:
                        harmonic_bw = qoe_log["cur_download_bw"]
                    else:
                        harmonic_bw = qoe_log["cur_download_bw"] * (cur_user_num / (cur_user_num + 1))

                else:
                    harmonic_bw = qoe_log["cur_download_bw"]
            elif ho_index == position:
                next_user_num = qoe_log["next_user_num"]
                if target_cur_sat_id == qoe_log["next_sat_id"] and index >= target_last_index + target_ho_index:
                    if next_user_num <= 1:
                        harmonic_bw = qoe_log["next_download_bw"]
                    else:
                        harmonic_bw = qoe_log["next_download_bw"] * (next_user_num / (next_user_num - 1))
                elif target_next_sat_id == qoe_log["next_sat_id"] and index >= target_last_index + target_ho_index:
                    if next_user_num < 1:
                        harmonic_bw = qoe_log["next_download_bw"]
                    else:
                        harmonic_bw = qoe_log["next_download_bw"] * (next_user_num / (next_user_num + 1))
                else:
                    harmonic_bw = qoe_log["next_download_bw"]

                # harmonic_bw = qoe_log["next_download_bw"]
                # Give them a penalty
                download_time += HANDOVER_DELAY
            else:
                next_user_num = qoe_log["next_user_num"]
                if target_cur_sat_id == qoe_log["next_sat_id"] and index >= target_last_index + target_ho_index:
                    if next_user_num <= 1:
                        harmonic_bw = qoe_log["next_download_bw"]
                    else:
                        harmonic_bw = qoe_log["next_download_bw"] * (next_user_num / (next_user_num - 1))
                elif target_next_sat_id == qoe_log["next_sat_id"] and index >= target_last_index + target_ho_index:
                    if next_user_num < 1:
                        harmonic_bw = qoe_log["next_download_bw"]
                    else:
                        harmonic_bw = qoe_log["next_download_bw"] * (next_user_num / (next_user_num + 1))
                else:
                    harmonic_bw = qoe_log["next_download_bw"]
                # harmonic_bw = qoe_log["next_download_bw"]
            download_time += (self.video_size[chunk_quality][index] / B_IN_MB) \
                             / harmonic_bw * BITS_IN_BYTE  # this is MB/MB/s --> seconds

            if curr_buffer < download_time:
                curr_rebuffer_time += (download_time -
                                       curr_buffer)
                curr_buffer = 0.0
            else:
                curr_buffer -= download_time
            curr_buffer += VIDEO_CHUNCK_LEN / MILLISECONDS_IN_SECOND

            # bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
            # smoothness_diffs += abs(VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
            bitrate_sum += VIDEO_BIT_RATE[chunk_quality]
            smoothness_diffs += abs(
                VIDEO_BIT_RATE[chunk_quality] - VIDEO_BIT_RATE[last_quality])
            last_quality = chunk_quality
        # compute reward for this combination (one reward per 5-chunk combo)

        # bitrates are in Mbits/s, rebuffer in seconds, and smoothness_diffs in Mbits/s

        # 10~140 - 0~100 - 0~130
        reward = bitrate_sum * QUALITY_FACTOR / M_IN_K - (REBUF_PENALTY * curr_rebuffer_time) \
                 - SMOOTH_PENALTY * smoothness_diffs / M_IN_K

        return reward

    def update_sat_info(self, sat_id, mahimahi_ptr, agent, variation):
        # update sat info

        if variation == 1:
            self.cur_satellite[sat_id].add_ue(agent)
        elif variation == -1:
            self.cur_satellite[sat_id].remove_ue(agent)

        if sat_id in self.num_of_user_sat.keys():
            self.num_of_user_sat[sat_id] += variation
        else:
            self.num_of_user_sat[sat_id] = variation
        # print(self.num_of_user_sat)
        assert self.num_of_user_sat[sat_id] >= 0

    def get_num_of_user_sat(self, sat_id):
        # update sat info
        if sat_id == "all":
            filtered_num_of_user_sat = {}
            for tmp_sat_id in self.num_of_user_sat.keys():
                if self.num_of_user_sat[tmp_sat_id] != 0:
                    filtered_num_of_user_sat[tmp_sat_id] = self.num_of_user_sat[tmp_sat_id]
            return filtered_num_of_user_sat
        if sat_id in self.num_of_user_sat.keys():
            return self.num_of_user_sat[sat_id]

        return 0

    def set_satellite(self, agent, sat=0, id_list=None):
        """
        if id_list is None:
            id_list = self.next_sat_id[agent]

        # Do not do any satellite switch
        sat_id = id_list[sat]
        """
        if id_list is None:
            sat_id = self.next_sat_id[agent]

        if sat == 1:
            if sat_id == self.cur_sat_id[agent]:
                # print("Can't do handover. Only one visible satellite")
                return
            self.update_sat_info(sat_id, self.mahimahi_ptr[agent], agent, 1)
            self.update_sat_info(self.cur_sat_id[agent], self.mahimahi_ptr[agent], agent, -1)
            self.cur_sat_id[agent] = sat_id
            self.delay[agent] = HANDOVER_DELAY
            # self.sat_decision_log[agent].append(sat_id)
            return sat_id

    def froze_num_of_user_sat(self):
        self.stored_num_of_user_sat = copy.deepcopy(self.num_of_user_sat)
        self.stored_mahimahi_ptr = copy.deepcopy(self.mahimahi_ptr)
        self.stored_last_mahimahi_time = copy.deepcopy(self.last_mahimahi_time)
        self.stored_buffer_size = copy.deepcopy(self.buffer_size)
        self.stored_video_chunk_counter = copy.deepcopy(self.video_chunk_counter)
        self.stored_cur_sat_id = copy.deepcopy(self.cur_sat_id)
        self.stored_cur_satellite = copy.deepcopy(self.cur_satellite)

    def restore_num_of_user_sat(self):
        self.num_of_user_sat = self.stored_num_of_user_sat
        self.mahimahi_ptr = self.stored_mahimahi_ptr
        self.last_mahimahi_time = self.stored_last_mahimahi_time
        self.buffer_size = self.stored_buffer_size
        self.video_chunk_counter = self.stored_video_chunk_counter
        self.cur_sat_id = self.stored_cur_sat_id
        self.stored_cur_satellite = copy.deepcopy(self.cur_satellite)

