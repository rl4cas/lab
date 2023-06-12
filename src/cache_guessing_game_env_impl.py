# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

# Author: Mulong Luo
# date 2021.12.3
# description: environment for study RL for side channel attack
from calendar import c
from collections import deque

import numpy as np
import random
import os
import yaml, logging
import sys
import replacement_policy
from itertools import permutations
from cache_simulator import print_cache

import gym
from gym import spaces

from omegaconf.omegaconf import open_dict

from cache_simulator import *

import time

"""
Description:
  A L1 cache with total_size, num_ways 
  assume cache_line_size == 1B

Observation:
  # let's book keep all obvious information in the observation space 
     since the agent is dumb
     it is a 2D matrix
    self.observation_space = (
      [
        3,                                          #cache latency
        len(self.attacker_address_space) + 1,       # last action
        self.window_size + 2,                       #current steps
        2,                                          #whether the victim has accessed yet
      ] * self.window_size
    )

Actions:
  action is one-hot encoding
  v = | attacker_addr | v | victim_guess_addr |

Reward:

Starting state:
  fresh cache with nolines

Episode termination:
  when the attacker make a guess
  when there is length violation
  when there is guess before victim violation
  episode terminates
"""
class CacheGuessingGameEnv(gym.Env):
  metadata = {'render.modes': ['human']}
  def __init__(self, env_config={
   "length_violation_reward":-10000,
   "double_victim_access_reward": -10000,
   "victim_access_reward":-10,
   "correct_reward":200,
   "wrong_reward":-9999,
   "step_reward":-1,
   "window_size":0,
   "attacker_addr_s":4,
   "attacker_addr_e":7,
   "victim_addr_s":0,
   "victim_addr_e":3,
   "allow_victim_multi_access": True,
   "verbose":0,
   "reset_limit": 1,    # specify how many reset to end an epoch?????
   "cache_configs": {
      # YAML config file for cache simulaton
      "architecture": {
        "word_size": 1, #bytes
        "block_size": 1, #bytes
        "write_back": True
      },
      "cache_1": {#required
        "blocks": 4, 
        "associativity": 1,  
        "hit_time": 1 #cycles
      },
      "mem": {#required
        "hit_time": 1000 #cycles
      }
    }
  }
):

    # enable HPC-based-detection escaping setalthystreamline
    self.length_violation_reward = env_config["length_violation_reward"] if "length_violation_reward" in env_config else -10000
    self.victim_access_reward = env_config["victim_access_reward"] if "victim_access_reward" in env_config else -10
    self.correct_reward = env_config["correct_reward"] if "correct_reward" in env_config else 200
    self.wrong_reward = env_config["wrong_reward"] if "wrong_reward" in env_config else -9999
    self.step_reward = env_config["step_reward"] if "step_reward" in env_config else 0
    self.reset_limit = env_config["reset_limit"] if "reset_limit" in env_config else 1
    self.cache_state_reset = env_config["cache_state_reset"] if "cache_state_reset" in env_config else True
    window_size = env_config["window_size"] if "window_size" in env_config else 0
    attacker_addr_s = env_config["attacker_addr_s"] if "attacker_addr_s" in env_config else 4
    attacker_addr_e = env_config["attacker_addr_e"] if "attacker_addr_e" in env_config else 7
    victim_addr_s = env_config["victim_addr_s"] if "victim_addr_s" in env_config else 0
    victim_addr_e = env_config["victim_addr_e"] if "victim_addr_e" in env_config else 3
    self.verbose = env_config["verbose"] if "verbose" in env_config else 0
    self.super_verbose = env_config["super_verbose"] if "super_verbose" in env_config else 0
    self.logger = logging.getLogger()
    self.fh = logging.FileHandler('log')
    self.sh = logging.StreamHandler()
    self.logger.addHandler(self.fh)
    self.logger.addHandler(self.sh)
    self.fh_format = logging.Formatter('%(message)s')
    self.fh.setFormatter(self.fh_format)
    self.sh.setFormatter(self.fh_format)
    self.logger.setLevel(logging.INFO)
    if "cache_configs" in env_config:
      self.configs = env_config["cache_configs"]
    else:
      self.config_file_name = os.path.dirname(os.path.abspath(__file__))+'/../configs/config_simple_L1'
      self.config_file = open(self.config_file_name)
      self.logger.info('Loading config from file ' + self.config_file_name)
      self.configs = yaml.load(self.config_file, yaml.CLoader)
    self.vprint(self.configs)
    # cahce configuration
    self.num_ways = self.configs['cache_1']['associativity'] 
    self.cache_size = self.configs['cache_1']['blocks']
    self.reset_time = 0

    if "rep_policy" not in self.configs['cache_1']:
      self.configs['cache_1']['rep_policy'] = 'lru'
    
    '''
    check window size
    '''
    if window_size == 0:
      #self.window_size = self.cache_size * 8 + 8 #10 
      self.window_size = self.cache_size * 4 + 8 #10 
    else:
      self.window_size = window_size
    self.feature_size = 4

    '''
    instantiate the cache
    '''
    self.hierarchy = build_hierarchy(self.configs, self.logger)
    self.step_count = 0

    self.attacker_address_min = attacker_addr_s
    self.attacker_address_max = attacker_addr_e
    self.attacker_address_space = range(self.attacker_address_min,
                                  self.attacker_address_max + 1)  # start with one attacker cache line
    self.victim_address_min = victim_addr_s
    self.victim_address_max = victim_addr_e
    self.victim_address_space = range(self.victim_address_min,
                                self.victim_address_max + 1)  #
  
    '''
    define the action space
    '''
    # using tightened action space
    ####  # one-hot encoding
        # | attacker_addr | v | victim_guess_addr | 
    self.action_space = spaces.Discrete(
      len(self.attacker_address_space) + 1 + len(self.victim_address_space)
    )
    # change for student for add flush 
    '''
    define the observation space
    '''
    self.max_box_value = max(self.window_size + 2,  2 * len(self.attacker_address_space) + 1 + len(self.victim_address_space) + 1)#max(self.window_size + 2, len(self.attacker_address_space) + 1) 
    self.observation_space = spaces.Box(low=-1, high=self.max_box_value, shape=(self.window_size, self.feature_size))
    self.state = deque([[-1, -1, -1, -1]] * self.window_size)

    '''
    initilizate the environment configurations
    ''' 
    self.vprint('Initializing...')
    self.l1 = self.hierarchy['cache_1']
    self.lv = self.hierarchy['cache_1']

    self.current_step = 0
    self.victim_accessed = False
    self.victim_address = random.randint(self.victim_address_min, self.victim_address_max)
    self._randomize_cache()
    
  '''
  gym API: step
  this is the function that implements most of the logic
  '''
  def step(self, action):
    # print_cache(self.l1)

    self.vprint('Step...')
    info = {}

    '''
    upack the action to adapt to slightly different RL framework
    '''
    if isinstance(action, np.ndarray):
        action = action.item()

    '''
    parse the action
    '''
    original_action = action
    action = self.parse_action(original_action) 
    address = hex(action[0]+self.attacker_address_min)[2:]            # attacker address in attacker_address_space
    is_guess = action[1]                                              # check whether to guess or not
    is_victim = action[2]                                             # check whether to invoke victim
    is_flush = action[3]                                              # check whether to flush
    victim_addr = hex(action[4] + self.victim_address_min)[2:]        # victim address
    
    '''
    The actual stepping logic

    1. first cehcking if the length is over the window_size, if not, go to 2, otherwise terminate
    2. second checking if it is a victim access, if so go to 3, if not, go to 4
    3. check if the victim can be accessed according to these options, if so make the access, if not terminate
    4. check if it is a guess, if so, evaluate the guess, if not go to 5. if not, terminate
    5. do the access, first check if it is a flush, if so do flush, if not, do normal access
    '''
    # if self.current_step > self.window_size : # if current_step is too long, terminate
    if self.step_count >= self.window_size - 1:
      r = 2 #
      self.vprint("length violation!")
      reward = self.length_violation_reward #-10000 
      done = True
    else:
      if is_victim == True:
        r = 2 #
        self.victim_accessed = True
        if True: #self.configs['cache_1']["rep_policy"] == "plru_pl": no need to distinuish pl and normal rep_policy
          if self.victim_address <= self.victim_address_max:
            self.vprint("victim access (hex) %x " % self.victim_address)
            t, _ = self.lv.read(hex(self.victim_address)[2:], self.current_step)#, domain_id='v')
            t = t.time # do not need to lock again
          else:
            self.vprint("victim make a empty access!") # do not need to actually do something
            t = 1 # empty access will be treated as HIT??? does that make sense???
            #t = self.l1.read(str(self.victim_address), self.current_step).time 
        
        self.current_step += 1
        reward = self.victim_access_reward #-10
        done = False
      else:
        if is_guess == True:
          r = 2  #
          '''
          this includes two scenarios
          1. normal scenario
          2. empty victim access scenario: victim_addr parsed is victim_addr_e, 
          and self.victim_address is also victim_addr_e + 1
          '''
          if self.victim_accessed and victim_addr == hex(self.victim_address)[2:]:
              if victim_addr != hex(self.victim_address_max + 1)[2:]: 
                self.vprint("correct guess (hex) " + victim_addr)
              else:
                self.vprint("correct guess empty access!")
              reward = self.correct_reward # 200
              done = True
          else:
              if victim_addr != hex(self.victim_address_max + 1)[2:]:
                self.vprint("wrong guess (hex) " + victim_addr )
              else:
                self.vprint("wrong guess empty access!")
              reward = self.wrong_reward #-9999
              done = True
        elif is_flush == False:
          lat, _ = self.l1.read(hex(int('0x' + address, 16))[2:], self.current_step)#, domain_id='a')
          lat = lat.time # measure the access latency
          if lat > 500:
            self.vprint("access (hex) " + address + " miss")
            r = 1 # cache miss
          else:
            self.vprint("access (hex) " + address + " hit"  )
            r = 0 # cache hit
          self.current_step += 1
          reward = self.step_reward #-1 
          done = False
        else:   
          ### remove for student to add 
          self.l1.clflush(hex(int('0x' + address, 16))[2:], self.current_step)#, domain_id='X')
          self.vprint("clflush (hex) " + address )
          r = 2
          self.current_step += 1
          reward = self.step_reward
          done = False
    #return observation, reward, done, info
    if done == True and is_guess != 0:
      info["is_guess"] = True
      if reward > 0:
        info["guess_correct"] = True
      else:
        info["guess_correct"] = False
    else:
      info["is_guess"] = False

    # the observation (r.time) in this case 
    # must be consistent with the observation space
    # return observation, reward, done?, info
    #return r, reward, done, info
    current_step = self.current_step
    if self.victim_accessed == True:
      victim_accessed = 1
    else:
      victim_accessed = 0
    
    '''
    append the current observation to the sliding window
    '''
    self.state.append([r, victim_accessed, original_action, self.step_count])
    self.state.popleft()

    self.step_count += 1
    
    if self.super_verbose == True:
      for cache in self.hierarchy:
        if self.hierarchy[cache].next_level:
          print_cache(self.hierarchy[cache])

    return np.array(list(reversed(self.state))), reward, done, info

  '''
  Gym API: reset the cache state
  '''
  def reset(self,
            victim_address=-1,
            reset_cache_state=False,
            reset_observation=True,
            seed = -1):

    self.vprint('Reset...(also the cache state)')
    self.hierarchy = build_hierarchy(self.configs, self.logger)
    self.l1 = self.hierarchy['cache_1']
    # check multicore
    self.lv = self.hierarchy['cache_1']
    self._randomize_cache()
    self._reset(victim_address)  # fake reset

    '''
    reset the observation space
    '''
    if reset_observation:
        self.state = deque([[-1, -1, -1, -1]] * self.window_size)
        self.step_count = 0

    self.reset_time = 0

    if self.super_verbose == True:
      for cache in self.hierarchy:
        if self.hierarchy[cache].next_level:
          print_cache(self.hierarchy[cache])

    return np.array(list(reversed(self.state)))

  '''
  fake reset the environment, just set a new victim addr 
  the actual physical state of the cache does not change
  '''
  def _reset(self, victim_address=-1):
    self.current_step = 0
    self.victim_accessed = False
    self.victim_address = random.randint(self.victim_address_min, self.victim_address_max)
    if self.victim_address <= self.victim_address_max:
      self.vprint("victim address (hex) " + hex(self.victim_address))
    else:
      self.vprint("victim has empty access")

  '''
  randomize the cache so that the attacker has to do a prime step
  '''
  def _randomize_cache(self, mode="union", seed=-1):
    # use seed so that we can get identical initialization states
    if seed != -1:
      random.seed(seed)
    if mode == "attacker":
      self.l1.read(hex(0)[2:])
      self.l1.read(hex(1)[2:])
      return
    if mode == "none":
      return
    self.current_step = -self.cache_size * 2 
    for _ in range(self.cache_size * 2):
      if mode == "victim":
        addr = random.randint(self.victim_address_min, self.victim_address_max)
      elif mode == "attacker":
        addr = random.randint(self.attacker_address_min, self.attacker_address_max)
      elif mode == "union":
        addr = random.randint(self.victim_address_min, self.victim_address_max) if random.randint(0,1) == 1 else random.randint(self.attacker_address_min, self.attacker_address_max)
      elif mode == "random":
        addr = random.randint(0, sys.maxsize)
      else:
        raise RuntimeError from None
      self.l1.read(hex(addr)[2:], self.current_step)#, domain_id='X')
      self.current_step += 1

  '''
  same as print() when self.verbose == 1
  otherwise does not do anything
  '''
  def vprint(self, *args):
    if self.verbose == 1:
      print( " "+" ".join(map(str,args))+" ")

  '''
  parse the action in the degenerate space (no redundant actions)
  returns list of 5 elements representing
  address, is_guess, is_victim, is_flush, victim_addr
  '''
  def parse_action(self, action):
    address = 0
    is_guess = 0
    is_victim = 0
    # need student to add flush instruction
    is_flush = 0
    victim_addr = 0 
    if action < len(self.attacker_address_space):
      address = action
    elif action == len(self.attacker_address_space):
      is_victim = 1
    else:
      is_guess = 1
      victim_addr = action - ( len(self.attacker_address_space) + 1 ) 
    return [ address, is_guess, is_victim, is_flush, victim_addr ] 
 