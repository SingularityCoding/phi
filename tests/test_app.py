from phi.ui.app import PhiApp


async def test_app_starts():
    app = PhiApp()
    async with app.run_test() as pilot:
        await pilot.pause()
