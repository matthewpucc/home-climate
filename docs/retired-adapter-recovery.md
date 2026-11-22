# Retire a disconnected heat-pump adapter

Use this only after physically replacing an adapter during interrupted recovery, not during a Wi-Fi dropout.

1. Switch Home Climate's **Controller Off** and wait for that intent to save and relevant device repair, stop and command work to finish.
2. Physically verify that the old adapter is disconnected and both entities serve the same head. The old entity must be unavailable and no longer bound. The replacement must already be the current unique head binding and report fresh native Off after the saved interruption. Do not use Developer Tools **Set state** to simulate feedback.
3. As a Home Assistant administrator, run action **house_climate.retire_recovery_adapter** directly under **Developer Tools → Actions**. Pass the old climate entity ID as **retired_entity_id**, the new climate entity ID as **replacement_entity_id**, and set **confirm_disconnected_same_head** to **true** only after the physical verification.

The action sends no equipment commands and leaves unrelated recovery and safety obligations in place. Positive recovery presets stay blocked across restart until you explicitly choose **Controller On**; the usual shared-head Off and ownership interlocks still apply. Saved blower experiments involving either adapter need separate review.

On the first startup claim after retirement, Home Climate avoids a redundant
Off command to the exact replacement if it still confirms native Off with no
pending or ambiguous commands. If Home Assistant restarted, the saved binding
is only a candidate: the matching replacement must provide a fresh native
Off report after Home Climate loads. If that proof is missing or changes,
the ordinary Off request and fresh acknowledgment are required. The other
heads' shutdown and the normal shared minimum-Off/settle timing do not change.
