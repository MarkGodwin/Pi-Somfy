#!/usr/bin/python3
# -*- coding: utf-8 -*-
#

import sys, re, argparse
import fcntl
import os
import re
import time
import locale
import pigpio
import socket
import signal, atexit, subprocess, traceback
import threading
import json
from copy import deepcopy

try:
    # pip3 install paho-mqtt
    from mylog import MyLog
    import paho.mqtt.client as paho
except Exception as e1:
    print("\n\nThis program requires the modules located from the same github repository that are not present.\n")
    print("Error: " + str(e1))
    sys.exit(2)


class DiscoveryMsg():
    DISCOVERY_MSG = {"name": "",
                     "availability_topic": "somfy/%s/service_status",
                     "payload_available": "online",
                     "payload_not_available": "offline",
                     "state_topic": "somfy/%s/state",
                     "state_opening": "opening",
                     "state_closing": "closing",
                     "state_stopped": "stopped",
                     "command_topic": "somfy/%s/state/cmd",
                     "payload_open": "open",
                     "payload_close": "close",
                     "payload_stop": "stop",
                     "position_topic": "somfy/%s/position",
                     "set_position_topic": "somfy/%s/position/set",
                     "unique_id": "",
                     "device": {"name": "",
                                "model": "Pi-Somfy controlled shutter",
                                "manufacturer": "Nickduino",
                                "identifiers": ""
                                }
                     }

    def __init__(self, shutter, shutterId, clientId):
        self.discovery_msg = deepcopy(DiscoveryMsg.DISCOVERY_MSG)
        self.discovery_msg["name"] = shutter
        self.discovery_msg["availability_topic"] = DiscoveryMsg.DISCOVERY_MSG["availability_topic"] % clientId
        self.discovery_msg["command_topic"] = DiscoveryMsg.DISCOVERY_MSG["command_topic"] % shutterId
        self.discovery_msg["state_topic"] = DiscoveryMsg.DISCOVERY_MSG["state_topic"] % shutterId
        self.discovery_msg["position_topic"] = DiscoveryMsg.DISCOVERY_MSG["position_topic"] % shutterId
        self.discovery_msg["set_position_topic"] = DiscoveryMsg.DISCOVERY_MSG["set_position_topic"] % shutterId
        self.discovery_msg["unique_id"] = shutterId
        self.discovery_msg["device"]["name"] = shutter
        self.discovery_msg["device"]["identifiers"] = shutterId

    def __str__(self):
        return json.dumps(self.discovery_msg)


class MQTT(threading.Thread, MyLog):
    connected_flag = False    
    
    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None):
        threading.Thread.__init__(self, group=group, target=target, name="MQTT")
        self.shutdown_flag = threading.Event()

        self.t = ()        
        self.args = args
        self.kwargs = kwargs
        if kwargs["log"] != None:
            self.log = kwargs["log"]
        if kwargs["shutter"] != None:
            self.shutter = kwargs["shutter"]
        if kwargs["config"] != None:
            self.config = kwargs["config"]
            
        return

    def receiveMessageFromMQTT(self, client, userdata, message):
        self.LogInfo("starting receiveMessageFromMQTT")
        try:
            msg = str(message.payload.decode("utf-8"))
            topic = message.topic
            self.LogInfo("message received from MQTT: "+topic+" = "+msg)
    
            [prefix, shutterId, prop, command] = topic.split("/")
            if(prop == "state"):
                if (command == "cmd"):
                    self.LogInfo("Shutter command: " + str(msg))
                    if msg == "stop":
                        self.shutter.stop(shutterId)
                    elif msg == "close":
                        self.shutter.lower(shutterId)
                    elif msg == "open":
                        self.shutter.rise(shutterId)
                    else:
                        self.LogInfo("Ignoring unknown command:" + str(msg))
            elif(prop == "position"):
                if (command == "set"):
                    self.LogInfo("Position will be set to: " + str(msg))
                    if int(msg) == 0:
                        self.shutter.lower(shutterId)
                    elif int(msg) == 100:
                        self.shutter.rise(shutterId)
                    elif (int(msg) > 0) and (int(msg) < 100):
                        currentPosition = self.shutter.getPosition(shutterId)
                        if int(msg) > currentPosition:
                            self.shutter.risePartial(shutterId, int(msg))
                        elif int(msg) < currentPosition:   
                            self.shutter.lowerPartial(shutterId, int(msg))
                        else:
                            self.LogInfo("Shutter is already at target position")
            else:
                self.LogError("received unkown message: "+topic+", message: "+msg)
    
        except Exception as e1:
            self.LogError("Exception Occured: " + str(e1))
    
        self.LogInfo("finishing receiveMessageFromMQTT")

    def sendMQTT(self, topic, msg):
        self.LogInfo("sending message to MQTT: " + topic + " = " + msg)
        self.t.publish(topic,msg,retain=True)
        
    def sendStartupInfo(self):
        for shutter, shutterId in sorted(self.config.ShuttersByName.items(), key=lambda kv: kv[1]):
            self.sendMQTT("homeassistant/cover/"+shutterId+"/config", str(DiscoveryMsg(shutter, shutterId, self.config.MQTT_ClientID)))

    def on_connect(self, client, userdata, flags, rc):
        if rc==0:
            self.LogInfo("Connected to MQTT with result code "+str(rc))
            self.connected_flag = True
            for shutter, shutterId in sorted(self.config.ShuttersByName.items(), key=lambda kv: kv[1]):
                self.LogInfo("Subscribe to shutter: "+shutter)
                self.t.subscribe("somfy/"+shutterId+"/position/set")
                self.t.subscribe("somfy/"+shutterId+"/state/cmd")
            if self.config.EnableDiscovery == True:
                self.LogInfo("Sending Home Assistant MQTT Discovery messages")
                self.sendStartupInfo()
                self.t.publish("somfy/" + self.config.MQTT_ClientID + "/service_status", "online", retain = True)

        else:
            print("Bad connection Returned code= ",rc)
            self.connected_flag=False
            
    def on_disconnect(self, client, userdata, rc=0):
        if rc != 0:
            self.LogInfo("Disconnected from MQTT Server. result code: " + str(rc))
            self.connected_flag=False
            while not self.connected_flag: #wait in loop
                self.LogInfo("Waiting 30sec for reconnect")
                time.sleep(30)
                self.t.connect(self.config.MQTT_Server,self.config.MQTT_Port)

            
    def set_position(self, shutterId, level):
        self.LogInfo("Publishing shutter "+shutterId+" position as "+str(level))
        self.sendMQTT("somfy/"+shutterId+"/position", str(level))

    # valid are opening, closing, stopped. TODO: should pass in an 
    def set_state(self, shutterId, state):
        self.LogInfo("Publishing shutter "+shutterId+" status as "+str(state))
        self.sendMQTT("somfy/"+shutterId+"/state", str(state))
            
    def run(self):
        self.connected_flag = False
        self.LogInfo("Entering MQTT polling loop")

        # Setup the mqtt client
        self.t = paho.Client(client_id=self.config.MQTT_ClientID)
        if not (self.config.MQTT_Password.strip() == ""):
           self.t.username_pw_set(username=self.config.MQTT_User,password=self.config.MQTT_Password)
        self.t.will_set("somfy/" + self.config.MQTT_ClientID + "/service_status", "offline", retain = True)
        self.t.on_connect = self.on_connect
        self.t.on_message = self.receiveMessageFromMQTT
        self.t.on_disconnect = self.on_disconnect
        self.shutter.registerPositionCallBack(self.set_position)
        self.shutter.registerStateCallBack(self.set_state)
        
        # Startup the mqtt listener
        error = 0
        while not self.shutdown_flag.is_set():
            # Loop until the server is available
            try:
                self.LogInfo("Connecting to MQTT server")
                self.t.connect(self.config.MQTT_Server,self.config.MQTT_Port)
                time.sleep(10)
                # if self.config.EnableDiscovery == True:
                #     self.sendStartupInfo()
                break
            except Exception as e:
                error += 1
                self.LogInfo("Exception in MQTT connect " + str(error) + ": "+ str(e.args))


        error = 0
        while not self.shutdown_flag.is_set():
            # Loop and poll for incoming requests
            try:
                #NOTE: Timeout value must be smaller than MQTT keep_alive (which is 60s by default)
                self.t.loop(timeout=30)
                # self.t.loop_start()
                if self.connected_flag == False:
                    time.sleep(10)
                    self.LogInfo("Re-Connecting to MQTT server")
                    self.t.connect(self.config.MQTT_Server,self.config.MQTT_Port)
            except Exception as e:
                error += 1
                self.LogInfo("Critical exception " + str(error) + ": "+ str(e.args))
                time.sleep(0.5) #Wait half a second when an exception occurs

        self.LogError("Received Signal to shut down MQTT thread")
        return

 
