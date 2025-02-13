"""Support for refoss sensors."""

from __future__ import annotations

import os
import json
import datetime
import asyncio

from collections.abc import Callable
from dataclasses import dataclass

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from refoss_ha.controller.electricity import ElectricityXMix

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .bridge import RefossDataUpdateCoordinator
from .const import (
    _LOGGER,
    CHANNEL_DISPLAY_NAME,
    COORDINATORS,
    DISPATCH_DEVICE_DISCOVERED,
    DOMAIN,
    SENSOR_EM,
)
from .entity import RefossEntity


@dataclass(frozen=True, kw_only=True)
class RefossSensorEntityDescription(SensorEntityDescription):
    """Describes Refoss sensor entity."""

    subkey: str
    fn: Callable[[float], float] = lambda x: x


DEVICETYPE_SENSOR: dict[str, str] = {
    "em06": SENSOR_EM,
    "em16": SENSOR_EM,
}

SENSORS: dict[str, tuple[RefossSensorEntityDescription, ...]] = {
    SENSOR_EM: (
        RefossSensorEntityDescription(
            key="power",
            translation_key="power",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfPower.WATT,
            suggested_display_precision=2,
            subkey="power",
            fn=lambda x: x / 1000.0,
        ),
        RefossSensorEntityDescription(
            key="voltage",
            translation_key="voltage",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
            suggested_display_precision=2,
            suggested_unit_of_measurement=UnitOfElectricPotential.VOLT,
            subkey="voltage",
        ),
        RefossSensorEntityDescription(
            key="current",
            translation_key="current",
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
            suggested_display_precision=2,
            suggested_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            subkey="current",
        ),
        RefossSensorEntityDescription(
            key="factor",
            translation_key="power_factor",
            device_class=SensorDeviceClass.POWER_FACTOR,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=2,
            subkey="factor",
        ),
        RefossSensorEntityDescription(
            key="energy",
            translation_key="this_month_energy",
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
            native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
            suggested_display_precision=2,
            subkey="mConsume",
            fn=lambda x: x,
        ),
        RefossSensorEntityDescription(
            key="this_day_energy",
            translation_key="this_day_energy",
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
            native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
            suggested_display_precision=2,
            subkey="mConsume",
            fn=lambda x: x,
        ),
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Refoss device from a config entry."""
    
    user_reset_day = config_entry.data.get("user_reset_day", 24)
    device_reset_day = config_entry.data.get("device_reset_day", 1)

    async def save_user_reset(_):
        """Save energy consumption data at the specified time (24일 09:30:30)."""
        for coordinator in hass.data[DOMAIN][COORDINATORS]:
            device = coordinator.device
            if not isinstance(device, ElectricityXMix):
                continue

            device_name = device.dev_name
            file_path = f"/config/{device_name}_monthly_energy.json"

            energy_data = {}
            for channel in device.channels:
                device_value = device.get_value(channel, "mConsume") or 0
                energy_data[channel] = (-1 * device_value) if device_value is not None else 0  # ✅ -1 곱해서 저장

            try:
                with open(file_path, "w", encoding="utf-8") as file:
                    json.dump(energy_data, file, indent=4)
                _LOGGER.info("Saved monthly energy data (inverted) for device %s", device_name)
            except IOError as e:
                _LOGGER.error("Failed to save monthly energy data: %s", e)
                
        await asyncio.sleep(3)  # ✅ 실행 후 3초 대기 (중복 실행 방지)
        schedule_user_reset()

    async def save_device_reset(_):
        """Save adjusted energy data at the specified time (28일 08:30:30)."""
        for coordinator in hass.data[DOMAIN][COORDINATORS]:
            device = coordinator.device
            if not isinstance(device, ElectricityXMix):
                continue

            device_name = device.dev_name
            file_path = f"/config/{device_name}_monthly_energy.json"

            energy_data = {}
            for channel in device.channels:
                # ✅ 센서 값 (기기 값 + 기존 파일 값) 저장
                device_value = device.get_value(channel, "mConsume") or 0
                stored_value = RefossSensor._cached_monthly_energy_data.get(str(channel), 0)
                adjusted_value = device_value + stored_value  # ✅ 센서 값으로 저장

                energy_data[channel] = adjusted_value

            try:
                with open(file_path, "w", encoding="utf-8") as file:
                    json.dump(energy_data, file, indent=4)
                _LOGGER.info("Saved adjusted energy data for device %s", device_name)
            except IOError as e:
                _LOGGER.error("Failed to save adjusted energy data: %s", e)

        await asyncio.sleep(3)  # ✅ 실행 후 3초 대기 (중복 실행 방지)
        schedule_device_reset()

    async def save_daily_energy(_):
        """Save daily energy consumption at midnight and update daily usage."""
        for coordinator in hass.data[DOMAIN][COORDINATORS]:
            device = coordinator.device
            if not isinstance(device, ElectricityXMix):
                continue
    
            device_name = device.dev_name
            file_path = f"/config/{device_name}_daily_energy.json"
    
            daily_energy_data = {}
    
            # ✅ 현재 센서 값을 가져와서 저장
            for channel in device.channels:
                device_value = device.get_value(channel, "mConsume") or 0 #기기값
                stored_value = RefossSensor._cached_monthly_energy_data.get(str(channel), 0) #월저장 파일값
                adjusted_value = device_value + stored_value  # ✅ 월사용량(기기+파일)
                
                if now.day == user_reset_day:
                    daily_energy_data[channel] = 0
                else:
                    daily_energy_data[channel] = adjusted_value
                    
                RefossSensor._cached_daily_energy_data[str(channel)] = adjusted_value  # ✅ 캐시 업데이트
                
            # ✅ 기존 파일 업데이트
            try:
                with open(file_path, "w", encoding="utf-8") as file:
                    json.dump(daily_energy_data, file, indent=4)
                _LOGGER.info("Updated daily energy file for device %s", device_name)
            except IOError as e:
                _LOGGER.error("Failed to update daily energy file: %s", e)
    
        await asyncio.sleep(3)  # ✅ 실행 후 3초 대기 (중복 실행 방지)
        schedule_daily_energy_save()
    
    def schedule_user_reset():
        """사용자 지정 리셋"""
        now = datetime.datetime.now()
        target_time = now.replace(day=user_reset_day, hour=0, minute=0, second=0)

        if now > target_time:
            target_time = target_time.replace(month=(now.month % 12) + 1, year=now.year + (1 if now.month == 12 else 0))

        _LOGGER.info("Next energy data save scheduled at: %s", target_time)
        async_track_point_in_time(hass, save_user_reset, target_time)

    def schedule_device_reset():
        """Refoss 자체 리렛"""
        now = datetime.datetime.now()
        target_time = now.replace(day=device_reset_day, hour=0, minute=0, second=0)

        if now > target_time:
            target_time = target_time.replace(month=(now.month % 12) + 1, year=now.year + (1 if now.month == 12 else 0))
            
        target_time = target_time - datetime.timedelta(seconds=1)

        _LOGGER.info("Next adjusted energy data save scheduled at: %s", target_time)
        async_track_point_in_time(hass, save_device_reset, target_time)

    def schedule_daily_energy_save():
        """Schedule daily energy saving at 00:00:00."""
        now = datetime.datetime.now()
        target_time = now.replace(hour=0, minute=0, second=0)
    
        if now > target_time:
            target_time = target_time + datetime.timedelta(days=1)
    
        _LOGGER.info("Next daily energy save scheduled at: %s", target_time)
        async_track_point_in_time(hass, save_daily_energy, target_time)
        
    schedule_user_reset()
    schedule_device_reset()
    schedule_daily_energy_save()


    @callback
    def init_device(coordinator: RefossDataUpdateCoordinator) -> None:
        """Register the device."""
        device = coordinator.device

        if not isinstance(device, ElectricityXMix):
            return

        sensor_type = DEVICETYPE_SENSOR.get(device.device_type, "")

        descriptions: tuple[RefossSensorEntityDescription, ...] = SENSORS.get(
            sensor_type, ()
        )

        async_add_entities(
            RefossSensor(
                coordinator=coordinator,
                channel=channel,
                description=description,
            )
            for channel in device.channels
            for description in descriptions
        )
        _LOGGER.debug("Device %s add sensor entity success", device.dev_name)

    for coordinator in hass.data[DOMAIN][COORDINATORS]:
        init_device(coordinator)

    config_entry.async_on_unload(
        async_dispatcher_connect(hass, DISPATCH_DEVICE_DISCOVERED, init_device)
    )


class EnergyFileWatcher(FileSystemEventHandler):
    """Watch for changes in the energy JSON files and reload data."""

    def __init__(self, sensor_instance, file_paths):
        """Initialize the file watcher for multiple files."""
        self._sensor_instance = sensor_instance
        self._file_paths = set(file_paths)  # ✅ 여러 개의 파일 감시 가능하도록 변경

    def on_modified(self, event):
        """Handle file modifications."""
        if event.src_path in self._file_paths:
            _LOGGER.info("Detected change in %s, reloading energy data.", event.src_path)

            # ✅ 파일 이름에 따라 적절한 로드 함수 호출
            if event.src_path.endswith("_daily_energy.json"):
                self._sensor_instance.load_daily_energy_data()
            elif event.src_path.endswith("_monthly_energy.json"):
                self._sensor_instance.load_energy_data()


class RefossSensor(RefossEntity, SensorEntity):
    """Refoss Sensor Device."""

    entity_description: RefossSensorEntityDescription
    _cached_monthly_energy_data = {}
    _cached_daily_energy_data = {}
    _observer = None
    
    def __init__(
        self,
        coordinator: RefossDataUpdateCoordinator,
        channel: int,
        description: RefossSensorEntityDescription,
    ) -> None:
        """Init Refoss sensor."""
        super().__init__(coordinator, channel)
        self.entity_description = description
        device_type = self.coordinator.device.device_type
        channel_alias = CHANNEL_DISPLAY_NAME.get(device_type, {}).get(channel, f"ch{channel}")
        self._attr_unique_id = f"{channel_alias}_{description.translation_key}"
        self._attr_name = f"{channel_alias}_{description.translation_key}"

        self.monthly_energy_file_path = f"/config/{self.coordinator.device.dev_name}_monthly_energy.json"
        self.daily_energy_file_path = f"/config/{self.coordinator.device.dev_name}_daily_energy.json"
        
        self.ensure_file_exists(self.monthly_energy_file_path, use_sensor_values=False)  # ✅ 0으로 저장 (monthly_energy.json)
        self.ensure_file_exists(self.daily_energy_file_path, use_sensor_values=True)  # ✅ 센서값 저장 (daily_energy.json)

        self.load_energy_data()
        self.load_daily_energy_data()
        self.start_watching_file()
        
    def ensure_file_exists(self, file_path, use_sensor_values=False):
        """Ensure the JSON file exists, creating it with appropriate initial values."""
        if not os.path.exists(file_path):
            try:
                if use_sensor_values:
                    # ✅ 현재 센서 값 (기기값) + 월사용량 값 저장 (daily_energy.json)
                    default_data = {}
                    for channel in self.coordinator.device.channels:
                        device_value = self.coordinator.device.get_value(channel, "mConsume") or 0
                        stored_value = RefossSensor._cached_monthly_energy_data.get(str(channel), 0)  # ✅ 월사용량 값 가져오기
                        default_data[str(channel)] = device_value + stored_value  # ✅ 기기값 + 월사용량 값 저장
                else:
                    # ✅ 모든 채널 값을 0으로 설정 (monthly_energy.json)
                    default_data = {str(channel): 0 for channel in self.coordinator.device.channels}
    
                with open(file_path, "w", encoding="utf-8") as file:
                    json.dump(default_data, file, indent=4)
    
                _LOGGER.info("Created new energy data file: %s with %s", 
                             file_path, "sensor values (device + monthly)" if use_sensor_values else "zero values")
    
            except IOError as e:
                _LOGGER.error("Failed to create energy data file: %s", e)


                
    def load_energy_data(self):
        """Load stored energy data from JSON file."""
        try:
            with open(self.monthly_energy_file_path, "r", encoding="utf-8") as file:
                RefossSensor._cached_monthly_energy_data = json.load(file)
            _LOGGER.info("Loaded stored energy data from %s", self.monthly_energy_file_path)
        except (IOError, json.JSONDecodeError):
            _LOGGER.error("Failed to read energy data file. Using default values.")
            # ✅ JSON 파일이 없거나 손상된 경우, 현재 기기의 채널 개수를 기반으로 기본값 설정
            RefossSensor._cached_monthly_energy_data = {str(channel): 0 for channel in self.coordinator.device.channels}

    def load_daily_energy_data(self):
        """Load stored daily energy data from JSON file into cache."""
        try:
            with open(self.daily_energy_file_path, "r", encoding="utf-8") as file:
                RefossSensor._cached_daily_energy_data = json.load(file)
            _LOGGER.info("Loaded daily energy data from %s", self.daily_energy_file_path)
        except (IOError, json.JSONDecodeError):
            _LOGGER.error("Failed to read daily energy data file. Using default values.")
            RefossSensor._cached_daily_energy_data = {str(channel): 0 for channel in self.coordinator.device.channels}

    def start_watching_file(self):
        """Start watching the energy JSON files for changes (only once)."""
        if RefossSensor._observer is not None:
            return  # ✅ 이미 감시 중이면 실행하지 않음
            
        file_paths = [self.monthly_energy_file_path, self.daily_energy_file_path]
        event_handler = EnergyFileWatcher(self, file_paths)
        
        RefossSensor._observer = Observer()
        RefossSensor._observer.schedule(event_handler, os.path.dirname(self.monthly_energy_file_path), recursive=False)
        RefossSensor._observer.start()
        _LOGGER.info("Started watching files: %s", file_paths)

    @property
    def native_value(self) -> StateType:
        """Return the native value including stored energy data."""
        device_value = self.coordinator.device.get_value(self.channel_id, self.entity_description.subkey) or 0 #기기값
        daily_stored_value = RefossSensor._cached_daily_energy_data.get(str(self.channel_id), 0) #일저장 파일값
        monthly_stored_value = RefossSensor._cached_monthly_energy_data.get(str(self.channel_id), 0) #월저장 파일값
    
        if self.entity_description.translation_key == "this_day_energy":
            return self.entity_description.fn(device_value + monthly_stored_value - daily_stored_value)  # ✅ 일사용량 센서
    
        if self.entity_description.translation_key == "this_month_energy":
            return self.entity_description.fn(device_value + monthly_stored_value)  # ✅ 월사용량 센서
    
        return self.entity_description.fn(device_value)  # ✅ 나머지 센서는 실시간 값만 반환
