#!/usr/bin/python3
# -*- coding: utf-8 -*-

import sys, re, argparse
import fcntl
import os
import locale
import time
import datetime
import ephem
import pigpio
import socket
import signal, atexit, traceback
import logging, logging.handlers
import threading

try:
    from myconfig import MyConfig
    from mylog import SetupLogger
    from mylog import MyLog
    from myscheduler import Event
    from myscheduler import Schedule
    from myscheduler import Scheduler
    from mywebserver import FlaskAppWrapper
    from myalexa import Alexa
    from mymqtt import MQTT
    from shutil import copyfile
except Exception as e1:
    print("\n\nThis program requires the modules located from the same github repository that are not present.\n")
    print("Error: " + str(e1))
    sys.exit(2)

class Shutter(MyLog):
    #Button values
    buttonUp = 0x2
    buttonStop = 0x1
    buttonDown = 0x4
    buttonProg = 0x8

    class ShutterState: # Definition of one shutter state
        status = 'stopped'
        position = 100 # as percentage: 0 = closed (down), 100 = open (up)
        startingPosition = 100
        lastStatusTime = None # get using time.monotonic()

        def __init__(self, initPosition = None):
            self.position = initPosition
            self.lastStatusTime = time.monotonic()

        def setStatus(self, status):
            self.status = status
            self.startingPosition = self.position
            self.lastStatusTime = time.monotonic()

    def __init__(self, log = None, config = None):
        super(Shutter, self).__init__()
        self.lock = threading.Lock()
        if log != None:
            self.log = log
        if config != None:
            self.config = config

        if self.config.TXGPIO != None:
           self.TXGPIO=self.config.TXGPIO # 433.42 MHz emitter
        else:
           self.TXGPIO=4 # 433.42 MHz emitter on GPIO 4
        self.frame = bytearray(7)
        self.positionCallback = []
        self.statusCallback = []
        self.shutterStateList = {}
        self.sutterStateLock = threading.Lock()

    def getShutterState(self, shutterId, initialPosition = None):
        with self.sutterStateLock:
            if shutterId not in self.shutterStateList:
                self.shutterStateList[shutterId] = self.ShutterState(initialPosition)
            return self.shutterStateList[shutterId]

    def getPosition(self, shutterId):
        state = self.getShutterState(shutterId, 0)
        return state.position

    def setPosition(self, shutterId, newPosition):
        state = self.getShutterState(shutterId)
        with self.sutterStateLock:
            state.position = newPosition
        for function in self.positionCallback:
            function(shutterId, newPosition)

        # Update the position of any shutters grouped with this one
        for childId in self.config.Shutters[shutterId]['groupedShutterIds']:
            self.setPosition(childId, newPosition)

    def setStatus(self, shutterId, status):
        state = self.getShutterState(shutterId)
        with self.sutterStateLock:
            state.setStatus(status)

        for function in self.statusCallback:
            function(shutterId, status)

        # Update the position of any shutters grouped with this one
        for childId in self.config.Shutters[shutterId]['groupedShutterIds']:
            self.setStatus(childId, status)

    def waitAndSetFinalPosition(self, shutterId, startingPosition, targetPosition):
        state = self.getShutterState(shutterId)
        oldlastStatusTime = state.lastStatusTime

        timeToWait = (abs(startingPosition - targetPosition)/100)*self.config.Shutters[shutterId]['duration']
        waited = 0

        self.LogDebug("["+self.config.Shutters[shutterId]['name']+"] Waiting for operation to complete for " + str(timeToWait) + " seconds")
        while timeToWait > waited:

            self.setPosition(shutterId, int(startingPosition + (targetPosition - startingPosition) * waited / timeToWait))
            time.sleep(min(timeToWait - waited,1))
            waited += 1

            # Cancel, if another command has been called in between
            if state.lastStatusTime != oldlastStatusTime:
                self.LogDebug("["+self.config.Shutters[shutterId]['name']+"] Discard final position. Position is now: " + str(state.position))
                return

        self.LogDebug("["+self.config.Shutters[shutterId]['name']+"] Blind has reached new final position: " + str(targetPosition))

        # Stop the shutter, if this is a partial movement (Risky if you're moving very close to the end of travel!)
        if targetPosition < 96 and targetPosition > 4:
            self.LogInfo("["+self.config.Shutters[shutterId]['name']+"] Stop at partial position requested")
            self.sendCommand(shutterId, self.buttonStop, self.config.SendRepeat)

        self.setPosition(shutterId, targetPosition)
        self.setStatus(shutterId, 'stopped')

    def lower(self, shutterId):
        state = self.getShutterState(shutterId, 100)

        self.LogInfo("["+self.config.Shutters[shutterId]['name']+"] Going down to the bottom")
        self.sendCommand(shutterId, self.buttonDown, self.config.SendRepeat)
        
        self.setStatus(shutterId, 'closing')

        # wait and set final position only if not interrupted in between
        t = threading.Thread(target = self.waitAndSetFinalPosition, args = (shutterId, state.startingPosition, 0))
        t.start()

    def lowerPartial(self, shutterId, percentage):
        state = self.getShutterState(shutterId, 100)

        self.LogInfo("["+self.config.Shutters[shutterId]['name']+"] Going down to" + str(percentage)) 
        self.sendCommand(shutterId, self.buttonDown, self.config.SendRepeat)
        self.setStatus(shutterId, 'closing')

        t = threading.Thread(target = self.waitAndSetFinalPosition, args = (shutterId, state.startingPosition, percentage))
        t.start()

    def rise(self, shutterId):
        state = self.getShutterState(shutterId, 0)

        self.LogInfo("["+self.config.Shutters[shutterId]['name']+"] Going up to the top")
        self.sendCommand(shutterId, self.buttonUp, self.config.SendRepeat)
        self.setStatus(shutterId, 'opening')

        t = threading.Thread(target = self.waitAndSetFinalPosition, args = (shutterId, state.startingPosition, 100))
        t.start()

    def risePartial(self, shutterId, percentage):
        state = self.getShutterState(shutterId, 0)

        self.LogInfo("["+self.config.Shutters[shutterId]['name']+"] Going up to " + str(percentage))
        self.sendCommand(shutterId, self.buttonUp, self.config.SendRepeat)
        self.setStatus(shutterId, 'opening')

        t = threading.Thread(target = self.waitAndSetFinalPosition, args = (shutterId, state.startingPosition, percentage))
        t.start()

    def stop(self, shutterId):
        state = self.getShutterState(shutterId, 50)

        self.LogInfo("["+self.config.Shutters[shutterId]['name']+"] Stopping")
        self.sendCommand(shutterId, self.buttonStop, self.config.SendRepeat)

        self.LogDebug("["+shutterId+"] Previous position: " + str(state.startingPosition))
        secondsSinceLastCommand = int(round(time.monotonic() - state.lastStatusTime))
        self.LogDebug("["+shutterId+"] Seconds since last command: " + str(secondsSinceLastCommand))

        # Compute position based on time elapsed since last command & command direction
        setupDuration = self.config.Shutters[shutterId]['duration']

        fallback = False
        if state.status == "stopped":
            self.LogInfo("["+shutterId+"] Stop pressed while stationary.")
            fallback = True
        elif secondsSinceLastCommand > 0 and secondsSinceLastCommand < setupDuration:
            # Work out how far we moved before we stopped
            durationPercentage = int(round(secondsSinceLastCommand/setupDuration * 100))
            self.LogDebug("["+shutterId+"] Duration percentage: " + str(durationPercentage) + ", Starting position: "+ str(state.startingPosition))
            if state.status == 'opening':
                newPosition = min (100 , state.startingPosition + durationPercentage)
            else:
                newPosition = max (0 , state.startingPosition - durationPercentage)
        else:  #improbable
            self.LogWarn("["+shutterId+"] Too much time since last command.")
            fallback = True

        if fallback == True: # Let's assume it will end on the intermediate position ! If it exists !
            intermediatePosition = self.config.Shutters[shutterId]['intermediatePosition']
            if (intermediatePosition == None) or (intermediatePosition == state.position):
                # The blind may be moving, but we have no idea
                self.LogInfo("["+shutterId+"] Intermediate position not defined. Assuming blind will stay stationary.")
                newPosition = state.position
            else:
                self.LogInfo("["+shutterId+"] Motor is probably moving to intermediate position "+str(intermediatePosition))
                if state.position > intermediatePosition:
                    self.setStatus(shutterId, 'closing')
                else:
                    self.setStatus(shutterId, 'opening')
                # wait and set final intermediate position only if not interrupted in between
                t = threading.Thread(target = self.waitAndSetFinalPosition, args = (shutterId, state.position, intermediatePosition))
                t.start()
                return

        # Save computed approximate position
        self.setPosition(shutterId, newPosition)
        self.setStatus(shutterId, 'stopped')

    # Push a set of buttons for a short or long press.
    def pressButtons(self, shutterId, buttons, longPress):
        self.sendCommand(shutterId, buttons, 35 if longPress else 1)

    def program(self, shutterId):
        self.sendCommand(shutterId, self.buttonProg, 1)

    def registerPositionCallBack(self, callbackFunction):
        self.positionCallback.append(callbackFunction)

    def registerStateCallBack(self, callbackFunction):
        self.statusCallback.append(callbackFunction)

    def sendCommand(self, shutterId, button, repetition): #Sending a frame
    # Sending more than two repetitions after the original frame means a button kept pressed and moves the blind in steps 
    # to adjust the tilt. Sending the original frame and three repetitions is the smallest adjustment, sending the original
    # frame and more repetitions moves the blinds up/down for a longer time.
    # To activate the program mode (to register or de-register additional remotes) of your Somfy blinds, long press the 
    # prog button (at least thirteen times after the original frame to activate the registration.
       self.LogDebug("sendCommand: Waiting for Lock")
       self.lock.acquire()
       try:
           self.LogDebug("sendCommand: Lock aquired")
           checksum = 0

           teleco = int(shutterId, 16)
           code = int(self.config.Shutters[shutterId]['code'])

           # print (codecs.encode(shutterId, 'hex_codec'))
           self.config.setCode(shutterId, code+1)

           pi = pigpio.pi() # connect to Pi

           if not pi.connected:
              exit()

           pi.wave_add_new()
           pi.set_mode(self.TXGPIO, pigpio.OUTPUT)

           self.LogInfo ("Remote  :      " + "0x%0.2X" % teleco + ' (' + self.config.Shutters[shutterId]['name'] + ')')
           self.LogInfo ("Button  :      " + "0x%0.2X" % button)
           self.LogInfo ("Rolling code : " + str(code))
           self.LogInfo ("")

           self.frame[0] = 0xA7;       # Encryption key. Doesn't matter much
           self.frame[1] = button << 4 # Which button did  you press? The 4 LSB will be the checksum
           self.frame[2] = code >> 8               # Rolling code (big endian)
           self.frame[3] = (code & 0xFF)           # Rolling code
           self.frame[4] = teleco >> 16            # Remote address
           self.frame[5] = ((teleco >>  8) & 0xFF) # Remote address
           self.frame[6] = (teleco & 0xFF)         # Remote address

           outstring = "Frame  :    "
           for octet in self.frame:
              outstring = outstring + "0x%0.2X" % octet + ' '
           self.LogInfo (outstring)

           for i in range(0, 7):
              checksum = checksum ^ self.frame[i] ^ (self.frame[i] >> 4)

           checksum &= 0b1111; # We keep the last 4 bits only

           self.frame[1] |= checksum;

           outstring = "With cks  : "
           for octet in self.frame:
              outstring = outstring + "0x%0.2X" % octet + ' '
           self.LogInfo (outstring)

           for i in range(1, 7):
              self.frame[i] ^= self.frame[i-1];

           outstring = "Obfuscated :"
           for octet in self.frame:
              outstring = outstring + "0x%0.2X" % octet + ' '
           self.LogInfo (outstring)

           #This is where all the awesomeness is happening. You're telling the daemon what you wanna send
           wf=[]
           wf.append(pigpio.pulse(1<<self.TXGPIO, 0, 9415)) # wake up pulse
           wf.append(pigpio.pulse(0, 1<<self.TXGPIO, 89565)) # silence
           for i in range(2): # hardware synchronization
              wf.append(pigpio.pulse(1<<self.TXGPIO, 0, 2560))
              wf.append(pigpio.pulse(0, 1<<self.TXGPIO, 2560))
           wf.append(pigpio.pulse(1<<self.TXGPIO, 0, 4550)) # software synchronization
           wf.append(pigpio.pulse(0, 1<<self.TXGPIO,  640))

           for i in range (0, 56): # manchester enconding of payload data
              if ((self.frame[int(i/8)] >> (7 - (i%8))) & 1):
                 wf.append(pigpio.pulse(0, 1<<self.TXGPIO, 640))
                 wf.append(pigpio.pulse(1<<self.TXGPIO, 0, 640))
              else:
                 wf.append(pigpio.pulse(1<<self.TXGPIO, 0, 640))
                 wf.append(pigpio.pulse(0, 1<<self.TXGPIO, 640))

           wf.append(pigpio.pulse(0, 1<<self.TXGPIO, 30415)) # interframe gap

           for j in range(1,repetition): # repeating frames
                    for i in range(7): # hardware synchronization
                          wf.append(pigpio.pulse(1<<self.TXGPIO, 0, 2560))
                          wf.append(pigpio.pulse(0, 1<<self.TXGPIO, 2560))
                    wf.append(pigpio.pulse(1<<self.TXGPIO, 0, 4550)) # software synchronization
                    wf.append(pigpio.pulse(0, 1<<self.TXGPIO,  640))

                    for i in range (0, 56): # manchester enconding of payload data
                          if ((self.frame[int(i/8)] >> (7 - (i%8))) & 1):
                             wf.append(pigpio.pulse(0, 1<<self.TXGPIO, 640))
                             wf.append(pigpio.pulse(1<<self.TXGPIO, 0, 640))
                          else:
                             wf.append(pigpio.pulse(1<<self.TXGPIO, 0, 640))
                             wf.append(pigpio.pulse(0, 1<<self.TXGPIO, 640))

                    wf.append(pigpio.pulse(0, 1<<self.TXGPIO, 30415)) # interframe gap
                    # time.sleep(0.25) # small pause before repetition, see issue #45

           pi.wave_add_generic(wf)
           wid = pi.wave_create()
           pi.wave_send_once(wid)
           while pi.wave_tx_busy():
              pass
           pi.wave_delete(wid)

           pi.stop()
       finally:
           self.lock.release()
           self.LogDebug("sendCommand: Lock released")

class operateShutters(MyLog):

    def __init__(self, args = None):
        super(operateShutters, self).__init__()
        self.ProgramName = "operate Somfy Shutters"
        self.Version = "Unknown"
        self.log = None
        self.IsStopping = False
        self.ProgramComplete = False

        if args.ConfigFile == None:
            self.ConfigFile = "/etc/operateShutters.conf"
        else:
            self.ConfigFile = args.ConfigFile

        self.console = SetupLogger("shutters_console", log_file = "", stream = True)

        if os.geteuid() != 0:
            self.LogConsole("You need to have root privileges to run this script.\nPlease try again, this time using 'sudo'.")
            sys.exit(1)

        if not os.path.isfile(self.ConfigFile):
            self.LogConsole("Creating new config file : " + self.ConfigFile)
            defaultConfigFile = os.path.dirname(os.path.realpath(__file__))+'/defaultConfig.conf'
            print(defaultConfigFile);
            if not os.path.isfile(defaultConfigFile):
                self.LogConsole("Failure to create new config file: "+defaultConfigFile)
                sys.exit(1)
            else: 
                copyfile(defaultConfigFile, self.ConfigFile)

        # read config file
        self.config = MyConfig(filename = self.ConfigFile, log = self.console)
        result = self.config.LoadConfig();
        if not result:
            self.LogConsole("Failure to load configuration parameters")
            sys.exit(1)

        # log errors in this module to a file
        self.log = SetupLogger("shutters", self.config.LogLocation + "operateShutters.log")
        self.config.log = self.log

        if self.IsLoaded():
            self.LogWarn("operateShutters.py is already loaded.")
            sys.exit(1)

        if not self.startPIGPIO():
            self.LogConsole("Not able to start PIGPIO")
            sys.exit(1)

        self.shutter = Shutter(log = self.log, config = self.config)

        # atexit.register(self.Close)
        # signal.signal(signal.SIGTERM, self.Close)
        # signal.signal(signal.SIGINT, self.Close)

        self.schedule = Schedule(log = self.log, config = self.config)
        self.scheduler = None
        self.webServer = None

        if (args.echo == True):
            self.alexa = Alexa(kwargs={'log':self.log, 'shutter': self.shutter, 'config': self.config})

        if (args.mqtt == True):
            self.mqtt = MQTT(kwargs={'log':self.log, 'shutter': self.shutter, 'config': self.config})

        self.ProcessCommand(args);

    #------------------------ operateShutters::IsLoaded -----------------------------
    #return true if program is already loaded
    def IsLoaded(self):

        file_path = '/var/lock/'+os.path.basename(__file__)
        global file_handle

        try:
           file_handle= open(file_path, 'w')
           fcntl.lockf(file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
           return False
        except IOError:
           return True

    #--------------------- operateShutters::startPIGPIO ------------------------------
    def startPIGPIO(self):
       if sys.version_info[0] < 3:
           import commands
           status, process = commands.getstatusoutput('sudo pidof pigpiod')
           if status:  #  it wasn't running, so start it
               self.LogInfo ("pigpiod was not running")
               commands.getstatusoutput('sudo pigpiod -l -m')  # try to  start it
               time.sleep(0.5)
               # check it again
               status, process = commands.getstatusoutput('sudo pidof pigpiod')
       else:
           import subprocess
           status, process = subprocess.getstatusoutput('sudo pidof pigpiod')
           if status:  #  it wasn't running, so start it
               self.LogInfo ("pigpiod was not running")
               subprocess.getstatusoutput('sudo pigpiod -l -m')  # try to  start it
               time.sleep(0.5)
               # check it again
               status, process = subprocess.getstatusoutput('sudo pidof pigpiod')

       if not status:  # if it was started successfully (or was already running)...
           pigpiod_process = process
           self.LogInfo ("pigpiod is running, process ID is {} ".format(pigpiod_process))

           try:
               pi = pigpio.pi()  # local GPIO only
               self.LogInfo ("pigpio's pi instantiated")
           except Exception as e:
               start_pigpiod_exception = str(e)
               self.LogError ("problem instantiating pi: {}".format(start_pigpiod_exception))
       else:
           self.LogError ("start pigpiod was unsuccessful.")
           return False
       return True

    #--------------------- operateShutters::ProcessCommand -----------------------------------------------
    def ProcessCommand(self, args):

       if ((args.long == True) and not (args.press)):
             print("ERROR: The -long option can only be specified with the -press option.\n")
             parser.print_help()
             
       elif ((args.shutterName != "") and (args.down == True)):
             self.shutter.lower(self.config.ShuttersByName[args.shutterName])
       elif ((args.shutterName != "") and (args.up == True)):
             self.shutter.rise(self.config.ShuttersByName[args.shutterName])
       elif ((args.shutterName != "") and (args.stop == True)):
             self.shutter.stop(self.config.ShuttersByName[args.shutterName])
       elif ((args.shutterName != "") and (args.program == True)):
             self.shutter.program(self.config.ShuttersByName[args.shutterName])
       elif ((args.shutterName != "") and (args.demo == True)):
             self.LogInfo ("lowering shutter for 7 seconds")
             self.shutter.lowerPartial(self.config.ShuttersByName[args.shutterName], 7)
             time.sleep(7)
             self.LogInfo ("rise shutter for 7 seconds")
             self.shutter.risePartial(self.config.ShuttersByName[args.shutterName], 7)
       elif ((args.shutterName != "") and (args.duskdawn is not None)):
             self.schedule.addRepeatEventBySunrise([self.config.ShuttersByName[args.shutterName]], 'up', args.duskdawn[1], ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'])
             self.schedule.addRepeatEventBySunset([self.config.ShuttersByName[args.shutterName]], 'down', args.duskdawn[0], ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'])
             self.scheduler = Scheduler(kwargs={'log':self.log, 'schedule':self.schedule, 'shutter': self.shutter, 'config': self.config})
             self.scheduler.setDaemon(True)
             self.scheduler.start()
             if (args.echo == True):
                 self.alexa.setDaemon(True)
                 self.alexa.start()
             if (args.mqtt == True):
                 self.mqtt.setDaemon(True)
                 self.mqtt.start()
             self.scheduler.join()
       elif ((args.shutterName != "") and (args.press)):

             buttons = 0

             btnMap = {
               'up': self.shutter.buttonUp,
               'down': self.shutter.buttonDown,
               'stop': self.shutter.buttonStop,
               'my': self.shutter.buttonStop,
               'program': self.shutter.buttonProg
             }
             for btn in args.press:
                 buttons |= btnMap[btn]

             self.shutter.pressButtons(self.config.ShuttersByName[args.shutterName], buttons, args.long)
       elif (args.auto == True):
             self.schedule.loadScheudleFromConfig()
             self.scheduler = Scheduler(kwargs={'log':self.log, 'schedule':self.schedule, 'shutter': self.shutter, 'config': self.config})
             self.scheduler.setDaemon(True)
             self.scheduler.start()
             if (args.echo == True):
                 self.alexa.setDaemon(True)
                 self.alexa.start()
             if (args.mqtt == True):
                 self.mqtt.setDaemon(True)
                 self.mqtt.start()
             self.webServer = FlaskAppWrapper(name='WebServer', static_url_path=os.path.dirname(os.path.realpath(__file__))+'/html', log = self.log, shutter = self.shutter, schedule = self.schedule, config = self.config)
             self.webServer.run()
       else:
          parser.print_help()

       if (args.echo == True):
           self.alexa.setDaemon(True)
           self.alexa.start()
       if (args.mqtt == True):
           self.mqtt.setDaemon(True)
           self.mqtt.start()

       if (args.echo == True):
           self.alexa.join()
       if (args.mqtt == True):
           self.mqtt.join()
       self.LogInfo ("Process Command Completed....")
       self.Close();

    #---------------------operateShutters::Close----------------------------------------
    def Close(self, signum = None, frame = None):

        # we dont really care about the errors that may be generated on shutdown
        try:
            self.IsStopping = True
        except Exception as e1:
            self.LogErrorLine("Error Closing Monitor: " + str(e1))

        self.LogError("operateShutters Shutdown")

        try:
            self.ProgramComplete = True
            if (not self.scheduler == None):
                self.LogError("Stopping Scheduler. This can take up to 1 second...")
                self.scheduler.shutdown_flag.set()
                self.scheduler.join()
                self.LogError("Scheduler stopped. Now exiting.")
            if (not self.alexa == None):
                self.LogError("Stopping Alexa Listener. This can take up to 1 second...")
                self.alexa.shutdown_flag.set()
                self.alexa.join()
                self.LogError("Alexa Listener stopped. Now exiting.")
            if (not self.mqtt == None):
                self.LogError("Stopping MQTT Listener. This can take up to 1 second...")
                self.mqtt.shutdown_flag.set()
                self.mqtt.join()
                self.LogError("MQTT Listener stopped. Now exiting.")
            if (not self.webServer == None):
                self.LogError("Stopping WebServer. This can take up to 1 second...")
                self.webServer.shutdown_server()
                self.LogError("WebServer stopped. Now exiting.")
            sys.exit(0)
        except:
            pass

#------------------- Command-line interface for monitor ------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='operate Somfy Shutters.')
    parser.add_argument('shutterName', nargs='?', help='Name of the Shutter')
    parser.add_argument('-config', '-c', dest='ConfigFile', default=os.getcwd()+'/operateShutters.conf', help='Name of the Config File (incl full Path)')
    parser.add_argument('-up', '-u', help='Raise the Shutter', action='store_true')
    parser.add_argument('-down', '-d', help='lower the Shutter', action='store_true')
    parser.add_argument('-stop', '-s', help='stop the Shutter', action='store_true')
    parser.add_argument('-program', '-p', help='program a new Shutter', action='store_true')
    parser.add_argument('-press', help='Simulate a press of the specified remote buttons (\'up\', \'down\', \'stop\'/\'my\', and \'program\'). You can specify multiple buttons to activate setup operations. This does not update the known state of the blinds, so should not be used for ordinary raise and lower operations.', metavar='BTN', nargs='+', type=str)
    parser.add_argument('-long', help='When used with the -press option, simulates a long press, instead of a short press.', action='store_true')
    parser.add_argument('-demo', help='lower the Shutter, Stop after 7 second, then raise the Shutter', action='store_true')
    parser.add_argument('-duskdawn', '-dd', type=int, nargs=2, help='Automatically lower the shutter at sunset and rise the shutter at sunrise, provide the evening delay and morning delay in minutes each')
    parser.add_argument('-auto', '-a', help='Run schedule based on config. Also will start up the web-server which can be used to setup the schedule. Try: https://'+socket.gethostname(), action='store_true')
    parser.add_argument('-echo', '-e', help='Enable Amazon Alexa (Echo) integration', action='store_true')
    parser.add_argument('-mqtt', '-m', help='Enable MQTT integration', action='store_true')
    args = parser.parse_args()

    #Start things up
    MyShutter = operateShutters(args = args)

    try:
        while not MyShutter.ProgramComplete:
            time.sleep(0.01)
        sys.exit(0)
    except:
        sys.exit(1)
