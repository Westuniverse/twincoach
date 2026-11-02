# SPDX-License-Identifier: GPL-2.0-only
from posepriv_coach.preview_world_arm_recovery import RecoveryArmDisplayDriver

def install_recovery(owner):
    """Private Full33 hook; install before the quality capture wrapper.

    Raw poses/images/selected/world observations remain in the returned work;
    only the inner geometry call sees the separate inferred display copy.
    """
    driver = RecoveryArmDisplayDriver(owner.asset,owner.world.rig.torso_length,
        enabled=True,damping_tau_ms=50.)
    original_prepare = owner.prepare

    def prepare(work):
        observations = work['world_observations']
        primary = observations[owner.view]
        other = observations['side' if owner.view=='front' else 'front']
        display,metadata = driver.apply(primary,other,
            ready_ns=max(o.source.timestamp_ns for o in observations.values()))
        geometry_work = dict(work)
        geometry_work['world_observations'] = dict(observations)
        geometry_work['world_observations'][owner.view] = display
        result = original_prepare(geometry_work)
        work['predictive_arms'] = metadata
        if 'scoring_view_mapping' in geometry_work:
            work['scoring_view_mapping'] = geometry_work['scoring_view_mapping']
        result['work'] = work
        result['predictive_arms'] = metadata
        return result

    owner.prepare = prepare
    owner.predictive_arm_driver = driver
    return owner
