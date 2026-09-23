/*
 * Copyright 2026 Scott Bezek
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#pragma once

#include "esphome/core/defines.h"

// USE_UPDATE is defined by codegen only when an update entity actually exists,
// so a fader with no firmware image configured pays nothing for this.
#ifdef USE_UPDATE

#include <string>

#include "esphome/components/update/update_entity.h"
#include "esphome/core/helpers.h"

namespace esphome {
namespace fader_buddy {

class FaderBuddy;

// Home Assistant's native firmware-update entity for one fader: installed
// version, available version, an install button and a progress bar, in the
// place HA already looks for firmware updates.
//
// Worth knowing when changing any of this: Home Assistant decides whether an
// update is *offered* from the two version strings alone (see
// UpdateStateResponse in api_connection.cpp -- there is no "available" flag on
// the wire, and no field for an error message either). UPDATE_STATE_AVAILABLE
// is only consulted locally, by the update.is_available condition. So the
// version strings are the contract, and anything we want a user to read has to
// go in them or in the hub's status text sensor.
//
// The entity is owned by the hub rather than being a Component of its own: the
// hub drives every state change from its own setup/poll/update state machine.
class FaderBuddyUpdate : public update::UpdateEntity, public Parented<FaderBuddy> {
 public:
  // HA's "install" button, and the update.perform action. force skips the
  // cheap cached go/no-go check (which is also what allows a downgrade).
  void perform(bool force) override;
  // HA's "check for updates". Cheap: re-reads REG_FW_VERSION, or re-probes a
  // fader that never answered, so a fader flashed over UPDI behind our back
  // doesn't stay misreported until the next reboot.
  void check() override;

  void set_title(const std::string &title) { this->update_info_.title = title; }
  void set_summary(const std::string &summary) { this->update_info_.summary = summary; }
  void set_release_url(const std::string &url) { this->update_info_.release_url = url; }
  void set_latest_version(const std::string &version) { this->update_info_.latest_version = version; }

  // Steady state: what the fader is running, and whether installing the
  // packaged image would change anything.
  void publish_versions(const std::string &current_version, bool available);
  // Mid-install, before there is a meaningful percentage (entering the
  // bootloader, erasing, verifying).
  void publish_installing();
  // Mid-install, streaming pages: coarse percentage, 0-100.
  void publish_progress(uint8_t pct);
};

}  // namespace fader_buddy
}  // namespace esphome

#endif  // USE_UPDATE
