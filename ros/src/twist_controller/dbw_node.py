#!/usr/bin/env python

from std_msgs.msg import Bool
from dbw_mkz_msgs.msg import ThrottleCmd, SteeringCmd, BrakeCmd, SteeringReport
from geometry_msgs.msg import TwistStamped
from styx_msgs.msg import Lane, Waypoint
from geometry_msgs.msg import PoseStamped
from twist_controller import Controller
import math
import tf
import numpy as np
import rospy

MPH2MPS       = 0.44704  # Conversion miles per hour to meters per second
RAD2DEG       = 57.2958  # radians to degrees (180/pi)
DEBUG         = False    # True = Print Statements appear in Terminal with Debug info

'''
You can build this node only after you have built (or partially built) the `waypoint_updater` node.
You will subscribe to `/twist_cmd` message which provides the proposed linear and angular velocities.
You can subscribe to any other message that you find important or refer to the document for list
of messages subscribed to by the reference implementation of this node.
One thing to keep in mind while building this node and the `twist_controller` class is the status
of `dbw_enabled`. While in the simulator, its enabled all the time, in the real car, that will
not be the case. This may cause your PID controller to accumulate error because the car could
temporarily be driven by a human instead of your controller.
We have provided two launch files with this node. Vehicle specific values (like vehicle_mass,
wheel_base) etc should not be altered in these files.
We have also provided some reference implementations for PID controller and other utility classes.
You are free to use them or build your own.
Once you have the proposed throttle, brake, and steer values, publish it on the various publishers
that we have created in the `__init__` function.
'''

class DBWNode(object):
    def __init__(self):
        rospy.init_node('dbw_node')

        self.vehicle_mass    = rospy.get_param('~vehicle_mass', 1736.35) # KG
        fuel_capacity   = rospy.get_param('~fuel_capacity', 13.5)   # GALS(?)
        brake_deadband  = rospy.get_param('~brake_deadband', .1)    #
        decel_limit     = rospy.get_param('~decel_limit', -5)       # M/S/S
        accel_limit     = rospy.get_param('~accel_limit', 1.)       # M/S/S
        self.wheel_radius    = rospy.get_param('~wheel_radius', 0.2413)  # M
        wheel_base      = rospy.get_param('~wheel_base', 2.8498)    # M
        steer_ratio     = rospy.get_param('~steer_ratio', 14.8)     # Unitless
        max_lat_accel   = rospy.get_param('~max_lat_accel', 3.)     # M/S/S
        max_steer_angle = rospy.get_param('~max_steer_angle', 8.)   # rad
        min_spd         = 10.0                                      # M/S
        
        self.steer_pub    = rospy.Publisher('/vehicle/steering_cmd',SteeringCmd, queue_size=1)
        self.throttle_pub = rospy.Publisher('/vehicle/throttle_cmd',ThrottleCmd, queue_size=1)
        self.brake_pub    = rospy.Publisher('/vehicle/brake_cmd',BrakeCmd, queue_size=1)

        # Create `TwistController` object
        self.controller = Controller()

        # Subscribe
        rospy.Subscriber('/final_waypoints', Lane, self.finalwpts_cb,queue_size=1)
        rospy.Subscriber('/vehicle/dbw_enabled', Bool, self.DBWEnabled_cb,queue_size=1)
        rospy.Subscriber('/current_pose', PoseStamped, self.CurrPose_cb,queue_size=1)
        rospy.Subscriber('/current_velocity', TwistStamped, self.CurrVel_cb,queue_size=1)
        rospy.Subscriber('/twist_cmd', TwistStamped, self.TwistCmd_cb,queue_size=1)
        
        self.dbw_enabled      = False
        self.current_velocity = None # Current Velocity - Units (?)
        self.finalwpts        = None # Final Waypoints  - Units (?)
        self.current_pose     = None # Current Pose     - Units (?)
        self.current_orient   = None #
        self.twist_cmd        = None # TwistCmd         - Units (?)
        self.CTE              = 0.0  # Cross Track Error
        self.heading_err      = 0.0  # heading error, in radians
        
        # store previous pass of throttle, steering and brake to reduce latency
        self.throttle_prev          = 0.0
        self.steer_prev             = 0.0
        self.brake_prev             = 0.0
        self.brake_new              = 0.0 # used for storing info between loops
        self.current_velocity_prev  = 0.0
        self.current_velocity_new   = 0.0 # used for storing info between loops
        self.prevtime               = rospy.get_time()

        self.loop()

    def loop(self):
        rate = rospy.Rate(10)
        while not rospy.is_shutdown():
            if self.finalwpts != None and self.current_velocity != None:
                proposed_linear_velocity   = self.twist_cmd.linear.x
                proposed_angular_velocity  = self.twist_cmd.angular.z
                current_linear_velocity    = self.current_velocity.linear.x
                current_angular_velocity    = self.current_velocity.angular.z
                self.CTE, self.heading_err = self.Compute_CTE()
                dbw_status                 = self.dbw_enabled 
                if DEBUG:
                    print('DBW NODE :: Vel Err             ', proposed_linear_velocity-current_linear_velocity)
                    print('DBW NODE :: CTE                 ', self.CTE)
                    print('DBW NODE :: Heading Err         ', (self.heading_err*RAD2DEG)) # heading error in degrees
                    print('DBW NODE :: Pure Pursuit AngVel ', proposed_angular_velocity)
                    print('DBW NODE :: DBW_STATUS          ', self.dbw_enabled)
                
                throttle, brake, steering = self.controller.control(proposed_linear_velocity,
                                                                    proposed_angular_velocity,
                                                                    current_linear_velocity,
                                                                    current_angular_velocity,
                                                                    self.CTE,
                                                                    proposed_angular_velocity,
                                                                    dbw_status)

                
                # update info for estimating decel
                self.brake_prev = self.brake_new
                self.brake_new = brake

                self.current_velocity_prev = self.current_velocity_new
                self.current_velocity_new = current_linear_velocity

                time = rospy.get_time()
                dt = time - self.prevtime
                self.prevtime = time

                if dt < 1e-3:
                    dt = 0.1

                # calculate decel measured and estimated using physical equations
                accel_meas = (self.current_velocity_new - self.current_velocity_prev) / dt
                decel_calc = - self.brake_prev / (self.wheel_radius * self.vehicle_mass) # a = F/m = T/(r*m)
                                                                    
                if dbw_status:
                    self.publish(throttle, brake, steering)

                    # print the predicted decel versus measured, if decelerating
                    if False: # self.brake_prev > 0.0:
                        if self.current_velocity_prev > MPH2MPS:
                            print('DBW NODE :: Decel calc ',decel_calc,' m/s^2')
                            print('DBW NODE :: Decel meas ',accel_meas,' m/s^2')
            rate.sleep()

    def publish(self, throttle, brake, steer):
        tcmd = ThrottleCmd()
        tcmd.enable = True
        tcmd.pedal_cmd_type = ThrottleCmd.CMD_PERCENT
        tcmd.pedal_cmd = throttle
        if self.throttle_prev != throttle:
            self.throttle_pub.publish(tcmd)
        #print('DBW NODE :: Throttle Command ',throttle)

        scmd = SteeringCmd()
        scmd.enable = True
        scmd.steering_wheel_angle_cmd = steer
        if self.steer_prev != steer:
            self.steer_pub.publish(scmd)
        #print('DBW NODE :: Steering Command ',steer)

        bcmd = BrakeCmd()
        bcmd.enable = True
        bcmd.pedal_cmd_type = BrakeCmd.CMD_TORQUE
        bcmd.pedal_cmd = brake
        if self.brake_prev != brake:
            self.brake_pub.publish(bcmd)
        #print('DBW NODE :: Brake Command ',brake)
        
    def finalwpts_cb(self, msg):
        self.finalwpts = msg.waypoints
        
    def DBWEnabled_cb(self, msg):
        self.dbw_enabled = msg.data
    
    def CurrPose_cb(self, msg):
        self.current_pose   = msg.pose.position
        self.current_orient = msg.pose.orientation

    def CurrVel_cb(self, msg):
        self.current_velocity = msg.twist
        
    def TwistCmd_cb(self, msg):
        self.twist_cmd = msg.twist
    
    def Compute_CTE(self):
        # Similar to the MPC project we need to convert from global coordinates to local car body coordinates
        # First translate and then rotate
        
        global_car_x = self.current_pose.x
        global_car_y = self.current_pose.y
        
        roll,pitch,yaw = tf.transformations.euler_from_quaternion([self.current_orient.x,self.current_orient.y,self.current_orient.z,self.current_orient.w])
        yaw = -1.*yaw # TODO: Need to invert the sign of yaw for things to work, not clear why
        
        x_car_body = []
        y_car_body = []
        
        for idx,wpt in enumerate(self.finalwpts):
            del_x = wpt.pose.pose.position.x - global_car_x
            del_y = wpt.pose.pose.position.y - global_car_y
            
            temp1 =  del_x*math.cos(yaw) - del_y*math.sin(yaw)
            temp2 =  del_x*math.sin(yaw) + del_y*math.cos(yaw)
            
            x_car_body.append(temp1)
            y_car_body.append(temp2)
        
        # As with MPC project, fit a 3rd order polynomial that fits most roads    
        coeff_3rd = np.polyfit(x_car_body,y_car_body,3)

        # TODO: take the derivative of the curve fit, to determine heading
        # slope = 3*coeff_3rd[0]*x*x + 2*coeff_3rd[1]*x + coeff_3rd[2]
        # at x = 0.0 simplifies to

        # QUESTION: should we really use the heading at x = 0.0 here,
        # or look at some future point along the curve?

        # NOTE: Heading error is not actually equal to angular velocity
        # This seems to perform ok, but is not strictly correct
        
        # Refer to Advanced Lane Finding : Measuring Curvature
        # The actual angular velocity that should be used in the yaw controller
        # is V / r (linear velocity divided curve radius)
        # Curve radius can be computed as (((1 + dx/dy)^2)^(3/2))/abs(d2x/dy2)
        
        slope = coeff_3rd[2]

        # Error between current heading and fitted heading at x = 0.0
        heading_err = math.atan2(slope, 1.0)

        # Limit to +/- 90 degrees?
        if heading_err > (math.pi/2):
            heading_err = math.pi/2 
        if heading_err < -(math.pi/2):
            heading_err = -(math.pi/2)
        
        CTE       = np.polyval(coeff_3rd,0.0)
        
        #Limit CTE to +/-5 which is empirically the limits of the lanes 0 and 2
        if CTE > 5.0:
            CTE = 5.0
        if CTE < -5.0:
            CTE = -5.0       
        
        return CTE, heading_err

if __name__ == '__main__':
    DBWNode()
