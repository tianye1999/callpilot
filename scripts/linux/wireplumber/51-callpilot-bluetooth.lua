-- WirePlumber 0.4.x: only Edge may claim phone-call audio.
bluez_monitor.properties["bluez5.hfphsp-backend"] = "none"
-- Do not expose an A2DP sink: phone media must stay on the phone.
bluez_monitor.properties["bluez5.roles"] = "[ a2dp_source ]"
