from cereal import log
from common.numpy_fast import clip, interp
from selfdrive.controls.lib.pid import PIController

LongCtrlState = log.ControlsState.LongControlState

STOPPING_EGO_SPEED = 1.6
MIN_CAN_SPEED = 0.3  # TODO: parametrize this in car interface
STOPPING_TARGET_SPEED = MIN_CAN_SPEED + 0.01
STARTING_TARGET_SPEED = 0.5
BRAKE_THRESHOLD_TO_PID = 0.2

STOPPING_BRAKE_RATE = 0.2  # brake_travel/s while trying to stop
STARTING_BRAKE_RATE = 0.6  # brake_travel/s while releasing on restart

BRAKE_STOPPING_TARGET_BP = [1.7, 1.2, .6, .4]
BRAKE_STOPPING_TARGET_D = [0.95,  0.85, .8, .8]  # apply at least this amount of brake to maintain the vehicle stationary

MAX_SPEED_ERROR_BP = [0., 5., 10.]  # speed breakpoints
MAX_SPEED_ERROR_V = [1.5, .5, .3]  # max positive v_pid error VS actual speed; this avoids controls windup due to slow pedal resp

RATE = 100.0


def long_control_state_trans(active, long_control_state, v_ego, v_target, v_pid,
                             output_gb, brake_pressed, cruise_standstill):
  """Update longitudinal control state machine"""
  stopping_condition = (v_ego < 2.0 and cruise_standstill) or \
                       (v_ego < STOPPING_EGO_SPEED and
                        ((v_pid < STOPPING_TARGET_SPEED and v_target < STOPPING_TARGET_SPEED) or
                        brake_pressed))

  starting_condition = v_target > STARTING_TARGET_SPEED and not cruise_standstill

  if (not active) or brake_pressed:
    long_control_state = LongCtrlState.off

  else:
    if long_control_state == LongCtrlState.off:
      if active:
        long_control_state = LongCtrlState.pid

    elif long_control_state == LongCtrlState.pid:
      if stopping_condition:
        long_control_state = LongCtrlState.stopping

    elif long_control_state == LongCtrlState.stopping:
      if starting_condition:
        long_control_state = LongCtrlState.starting

    elif long_control_state == LongCtrlState.starting:
      if stopping_condition:
        long_control_state = LongCtrlState.stopping
      elif output_gb >= -BRAKE_THRESHOLD_TO_PID:
        long_control_state = LongCtrlState.pid

  return long_control_state


class LongControl():
  def __init__(self, CP, compute_gb):
    self.long_control_state = LongCtrlState.off  # initialized to off
    self.pid = PIController((CP.longitudinalTuning.kpBP, CP.longitudinalTuning.kpV),
                            (CP.longitudinalTuning.kiBP, CP.longitudinalTuning.kiV),
                            rate=RATE,
                            sat_limit=0.8,
                            convert=compute_gb)
    self.v_pid = 0.0
    self.last_output_gb = 0.0

  def reset(self, v_pid):
    """Reset PID controller and change setpoint"""
    self.pid.reset()
    self.v_pid = v_pid

  def update(self, active, CS, v_target, v_target_future, a_target, CP):
    """Update longitudinal control. This updates the state machine and runs a PID loop"""
    # Actuation limits
    gas_max = interp(CS.vEgo, CP.gasMaxBP, CP.gasMaxV)
    brake_max = interp(CS.vEgo, CP.brakeMaxBP, CP.brakeMaxV)
    stop_decel = interp(CS.vEgo, BRAKE_STOPPING_TARGET_BP, BRAKE_STOPPING_TARGET_D)

    # Update state machine
    output_gb = self.last_output_gb
    self.long_control_state = long_control_state_trans(active, self.long_control_state, CS.vEgo,
                                                       v_target_future, self.v_pid, output_gb,
                                                       CS.brakePressed, CS.cruiseState.standstill)

    v_ego_pid = max(CS.vEgo, MIN_CAN_SPEED)  # Without this we get jumps, CAN bus reports 0 when speed < 0.3

    if self.long_control_state == LongCtrlState.off:
      self.v_pid = v_ego_pid
      self.pid.reset()
      output_gb = 0.

    # tracking objects and driving
    elif self.long_control_state == LongCtrlState.pid:
      self.v_pid = v_target
      self.pid.pos_limit = gas_max
      self.pid.neg_limit = - brake_max

      # Toyota starts braking more when it thinks you want to stop
      # Freeze the integrator so we don't accelerate to compensate, and don't allow positive acceleration
      prevent_overshoot = not CP.stoppingControl and CS.vEgo < 1.5 and v_target_future < 0.7
      deadzone = interp(v_ego_pid, CP.longitudinalTuning.deadzoneBP, CP.longitudinalTuning.deadzoneV)
#      accel_pos_error_max = interp((self.v_pid - v_ego_pid), MAX_SPEED_ERROR_BP, MAX_SPEED_ERROR_V)

#      # limit +ve set point to avoid i term windup during acceleration
#      if self.v_pid > (v_ego_pid + accel_pos_error_max):
#        self.v_pid = v_ego_pid + accel_pos_error_max

      output_gb = self.pid.update(self.v_pid, v_ego_pid, speed=v_ego_pid, deadzone=deadzone, feedforward=a_target, freeze_integrator=prevent_overshoot)

      if prevent_overshoot:
        output_gb = min(output_gb, 0.0)

    # Intention is to stop, switch to a different brake control until we stop
    elif self.long_control_state == LongCtrlState.stopping:
      # Keep applying brakes until the car is stopped
      if not CS.standstill or output_gb > -stop_decel:
        output_gb -= STOPPING_BRAKE_RATE / RATE
      output_gb = clip(output_gb, -brake_max, gas_max)

      self.v_pid = CS.vEgo
      self.pid.reset()

    # Intention is to move again, release brake fast before handing control to PID
    elif self.long_control_state == LongCtrlState.starting:
      if output_gb < -0.2:
        output_gb += STARTING_BRAKE_RATE / RATE
      self.v_pid = CS.vEgo
      self.pid.reset()

    self.last_output_gb = output_gb
    final_gas = clip(output_gb, 0., gas_max)
    final_brake = -clip(output_gb, -brake_max, 0.)

    return final_gas, final_brake
